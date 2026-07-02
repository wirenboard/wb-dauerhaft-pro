"""Blind device model: maps high-level actions to protocol frames, tracks state.

One :class:`Blind` per physical actuator. It owns no MQTT and no I/O loop; it uses
an injected transport (:class:`~wb_mqtt_dauerhaft_pro.transport.SerialTransport` or
a test fake) to talk to the device, and exposes plain methods + a :class:`BlindState`
that the MQTT layer publishes.

Position convention: on the wire 0 = fully down/closed, 100 = fully up/open. The
user-facing position can be inverted per device via ``reverse_position``.

Optional capabilities (slat angle, firmware query) are auto-detected: if the device
answers a query with an error frame, that capability is disabled so we stop asking.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import protocol
from .transport import DeviceTimeout, PortConfig, TransportError

logger = logging.getLogger(__name__)

RESP_SINGLE = 7  # addr+func+len+sub+value+crc2 for a one-value reply
RESP_FIRMWARE = 64  # match vendor driver; wb-mqtt-serial returns the short frame after the T3.5 gap

# Setting/address operations write to the device's flash and can take much longer
# to answer than a plain query. The vendor's wb-rules driver uses a 5 s budget for
# these (TX_PRIO_CONFIG); the default 500 ms is only enough for reads/motion.
CONFIG_TIMEOUT_MS = 5000


class BlindType(str, Enum):
    ROLLER = "roller"  # рулонная — no slats
    SLIDING = "sliding"  # раздвижной карниз — no slats
    LAMELLA = "lamella"  # tiltable slats — angle supported


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class BlindConfig:
    name: str
    address: int
    port: PortConfig
    blind_type: BlindType = BlindType.ROLLER
    reverse_position: bool = False
    poll_interval_s: float = 1.0
    offline_after_misses: int = 3  # mark offline only after this many consecutive missed polls


@dataclass
class BlindState:
    online: bool = False
    position: Optional[int] = None  # user-facing 0..100 (None if unknown / marker set)
    position_marker: Optional[str] = None  # e.g. "both_limits_unset"
    motion: Optional[protocol.MotorState] = None
    angle: Optional[int] = None  # degrees, only for lamella
    firmware: Optional[str] = None
    address: Optional[int] = None  # device's own address, from a query/change


class Blind:
    """High-level control + state for one Dauerhaft PRO actuator."""

    def __init__(self, config: BlindConfig, transport):
        self.cfg = config
        self._t = transport
        self.state = BlindState()
        # capabilities: lamella type may tilt; others don't. Cleared on error reply.
        self._angle_supported = config.blind_type == BlindType.LAMELLA
        self._firmware_supported = True
        self._miss_count = 0

    # ------------------------------------------------------------------ #
    # position mapping (user <-> device)
    # ------------------------------------------------------------------ #
    def _to_device_pos(self, user_pct: int) -> int:
        user_pct = _clamp(int(user_pct), 0, 100)
        return 100 - user_pct if self.cfg.reverse_position else user_pct

    def _to_user_pos(self, device_pct: int) -> int:
        return 100 - device_pct if self.cfg.reverse_position else device_pct

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def open(self):
        self._act(protocol.control_open(self.cfg.address))

    def close(self):
        self._act(protocol.control_close(self.cfg.address))

    def stop(self):
        self._act(protocol.control_stop(self.cfg.address))

    def set_position(self, user_pct: int):
        self._act(protocol.control_move(self.cfg.address, self._to_device_pos(user_pct)))

    def set_angle(self, degrees: int):
        self._act(protocol.control_angle(self.cfg.address, _clamp(int(degrees), 0, protocol.ANGLE_MAX)))

    def jog_up(self):
        self._act(protocol.control_jog_up(self.cfg.address))

    def jog_down(self):
        self._act(protocol.control_jog_down(self.cfg.address))

    def third_point(self):
        """Move to the stored third point (control 0x04 sub 0x03)."""
        self._act(protocol.control_third_point(self.cfg.address))

    # ------------------------------------------------------------------ #
    # configuration / service commands (setting 0x02, address 0x10)
    # ------------------------------------------------------------------ #
    def set_upper_limit(self):
        return self._act(protocol.setting(self.cfg.address, protocol.SettingSub.SET_UPPER_LIMIT),
                         total_timeout_ms=CONFIG_TIMEOUT_MS)

    def set_lower_limit(self):
        return self._act(protocol.setting(self.cfg.address, protocol.SettingSub.SET_LOWER_LIMIT),
                         total_timeout_ms=CONFIG_TIMEOUT_MS)

    def delete_limits(self):
        return self._act(protocol.setting(self.cfg.address, protocol.SettingSub.DELETE_LIMITS),
                         total_timeout_ms=CONFIG_TIMEOUT_MS)

    def set_third_point(self):
        """Store the current position as the third point (setting 0x02 sub 0x05)."""
        return self._act(protocol.setting(self.cfg.address, protocol.SettingSub.SET_THIRD_POINT),
                         total_timeout_ms=CONFIG_TIMEOUT_MS)

    def change_direction(self):
        return self._act(protocol.setting(self.cfg.address, protocol.SettingSub.CHANGE_DIRECTION),
                         total_timeout_ms=CONFIG_TIMEOUT_MS)

    def query_address(self):
        """Read the device's own address (returns int or None)."""
        resp = self._request(protocol.query_address(self.cfg.address), total_timeout_ms=CONFIG_TIMEOUT_MS)
        if isinstance(resp, protocol.QueryResponse) and resp.address is not None:
            self.state.address = resp.address
            return resp.address
        return None

    def set_address(self, new_address: int) -> bool:
        """Change THIS device's address (unicast, never broadcast).

        Guarded: refuses the broadcast/universal address 0. Sent to the device's
        current address, so only this one device changes. On success the runtime
        config follows the new address (so polling continues) and a warning is
        logged — the on-disk config must be updated to persist it.
        """
        if new_address == protocol.BROADCAST_ADDRESS:
            raise ValueError("refusing to assign address 0 (broadcast/universal)")
        if not 1 <= new_address <= 0xFF:
            raise ValueError("address must be 1..255")
        resp = self._request(protocol.set_address(self.cfg.address, new_address),
                             total_timeout_ms=CONFIG_TIMEOUT_MS)
        if isinstance(resp, protocol.SetAddressResponse) and resp.ok:
            logger.warning("%s: address changed 0x%02X -> 0x%02X; UPDATE THE CONFIG to persist it",
                           self.cfg.name, self.cfg.address, new_address)
            self.cfg.address = new_address  # follow at runtime so we keep polling this device
            self.state.address = new_address
            return True
        logger.warning("%s: address change to 0x%02X failed/unconfirmed", self.cfg.name, new_address)
        return False

    # ------------------------------------------------------------------ #
    # polling
    # ------------------------------------------------------------------ #
    def poll(self):
        """One poll cycle: refresh position, motion, and (if supported) angle/firmware."""
        pos = self._request(protocol.query_position(self.cfg.address))
        if pos is None:
            self._miss_count += 1
            if self._miss_count >= self.cfg.offline_after_misses:
                self.state.online = False
            return  # missed this cycle; don't hammer it with more queries
        self._miss_count = 0
        self.state.online = True
        self._update_state(pos)

        state = self._request(protocol.query_state(self.cfg.address))
        if state is not None:
            self._update_state(state)

        if self._angle_supported:
            angle = self._request(protocol.query_angle(self.cfg.address))
            if isinstance(angle, protocol.ErrorResponse):
                logger.info("%s: angle query unsupported, disabling", self.cfg.name)
                self._angle_supported = False
            elif angle is not None:
                self._update_state(angle)

        if self._firmware_supported and self.state.firmware is None:
            fw = self._request(protocol.query_firmware(self.cfg.address), RESP_FIRMWARE)
            if isinstance(fw, protocol.ErrorResponse):
                logger.info("%s: firmware query unsupported, disabling", self.cfg.name)
                self._firmware_supported = False
            elif fw is not None:
                self._update_state(fw)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _act(self, request: bytes, total_timeout_ms=None):
        """Send a control command and fold its (position-echoing) reply into state."""
        resp = self._request(request, total_timeout_ms=total_timeout_ms)
        if resp is not None:
            self.state.online = True
            self._miss_count = 0
            self._update_state(resp)
        return resp

    def _request(self, request: bytes, response_size: int = RESP_SINGLE, total_timeout_ms=None):
        """Send *request*, return the decoded response, or None on timeout/error."""
        try:
            reply = self._t.transceive(self.cfg.port, request, response_size,
                                       total_timeout_ms=total_timeout_ms)
        except DeviceTimeout:
            # A single query timing out does not by itself mean offline; poll()
            # decides liveness from the position query. Just report "no data".
            return None
        except TransportError as exc:
            logger.warning("%s: transport error: %s", self.cfg.name, exc)
            return None
        try:
            return protocol.decode_response(protocol.parse_frame(reply))
        except protocol.ProtocolError as exc:
            logger.warning("%s: bad frame %s: %s", self.cfg.name, reply.hex(), exc)
            return None

    def _update_state(self, resp):
        if isinstance(resp, protocol.QueryResponse):
            if resp.sub == protocol.QuerySub.POSITION:
                self._set_position(resp.position, resp.position_marker)
            elif resp.sub == protocol.QuerySub.STATE:
                self.state.motion = resp.state
            elif resp.sub == protocol.QuerySub.ANGLE:
                self.state.angle = resp.angle
            elif resp.sub == protocol.QuerySub.FIRMWARE:
                self.state.firmware = resp.firmware
        elif isinstance(resp, protocol.ControlResponse):
            if resp.position is not None or resp.position_marker is not None:
                self._set_position(resp.position, resp.position_marker)
            if resp.angle is not None:
                self.state.angle = resp.angle
        elif isinstance(resp, protocol.ActiveReport):
            self._set_position(resp.position, resp.position_marker)
            self.state.motion = resp.state

    def _set_position(self, device_pct, marker):
        if device_pct is not None:
            self.state.position = self._to_user_pos(device_pct)
            self.state.position_marker = None
        else:
            self.state.position = None
            self.state.position_marker = marker
