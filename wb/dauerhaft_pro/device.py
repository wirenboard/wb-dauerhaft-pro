"""
Actuator model: maps the supported actions to protocol frames and tracks liveness.

One :class:`Actuator` per physical device. It owns no MQTT and no I/O loop; it
uses an injected transport (:class:`~wb.dauerhaft_pro.transport.SerialTransport`
or a test fake) to talk to the device.

Supported operations:
  * :meth:`up` / :meth:`down` / :meth:`stop` / :meth:`set_angle_raw` /
    :meth:`go_third_point` / :meth:`set_third_point` — drive the motor;
  * :meth:`query_position` / :meth:`query_angle_raw` — read the state;
  * :meth:`set_address` / :meth:`set_address_broadcast` /
    :meth:`set_address_learning` — change an RS-485 address;
  * :meth:`ping` — read the device address, used as a liveness probe.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import protocol
from .transport import DeviceTimeout, PortConfig, TransportError

logger = logging.getLogger(__name__)

# A reply is addr+func+len + up to 2 data bytes + 2 CRC = 7 bytes for every
# supported command (control echoes, position/angle/address queries, setting
# and set-address acks, error reports). This is exact only because these
# commands have no variable-length replies; a firmware-version query (3 data
# bytes) or similar would need its own size, derived from the request.
RESP_SIZE = 7

# Changing the address or storing the waypoint writes the device's flash and
# answers slower than a plain move; the vendor driver allows several seconds.
FLASH_TIMEOUT_MS = 5000

# Motors ignore the bus for 0.5..1.2 s after a broadcast address write while
# they store it; hold our polling off for the worst case (vendor value).
BROADCAST_HOLD_S = 1.2

# expect_address value that accepts a reply from any bus address — for the
# broadcast/learning address writes, where the answering motor's address is not
# known in advance (the vendor driver disables its filter the same way).
ANY_ADDRESS = -1

# Mark a device offline only after this many consecutive missed exchanges, so a
# single transient RS-485 glitch — e.g. a collision with a chatty device's active
# report on a shared bus — does not flap the availability indicator.
OFFLINE_AFTER_MISSES = 3


@dataclass
class ActuatorConfig:
    device_id: str
    name: str
    curtain_type: str
    learning_type: str
    address: int
    port: PortConfig
    slat_angle_mode: str = "none"  # none (no slat controls) | direct | compressed


class Actuator:
    """
    High-level control + liveness for one Dauerhaft PRO actuator.
    """

    def __init__(self, config: ActuatorConfig, transport):
        self.cfg = config
        self._t = transport
        self.online = False
        self._miss_count = 0

    # ------------------------------------------------------------------ #
    # motion
    # ------------------------------------------------------------------ #
    def up(self):
        """
        Drive up / open.
        """
        self._exchange(protocol.control_up(self.cfg.address))

    def down(self):
        """
        Drive down / close.
        """
        self._exchange(protocol.control_down(self.cfg.address))

    def stop(self):
        """
        Stop motion.
        """
        self._exchange(protocol.control_stop(self.cfg.address))

    def set_angle_raw(self, raw_angle: int):
        """
        Rotate the slats to a raw wire byte (scale conversion is the caller's job).
        """
        self._exchange(protocol.control_angle(self.cfg.address, raw_angle))

    def go_third_point(self):
        """
        Drive to the stored waypoint; warns when the motor has none stored.
        """
        resp = self._exchange(protocol.control_third_point(self.cfg.address))
        if isinstance(resp, protocol.ControlResponse) and resp.value == protocol.THIRD_POINT_UNSET:
            logger.warning("%s: no waypoint is stored in the motor", self.cfg.device_id)

    def set_third_point(self):
        """
        Store the current position as the waypoint (writes the motor's flash).
        """
        resp = self._exchange(
            protocol.set_third_point(self.cfg.address),
            response_timeout_ms=FLASH_TIMEOUT_MS,
            total_timeout_ms=FLASH_TIMEOUT_MS,
        )
        if isinstance(resp, protocol.SettingResponse) and not resp.ok:
            logger.warning("%s: the motor refused to store the waypoint", self.cfg.device_id)

    # ------------------------------------------------------------------ #
    # state queries
    # ------------------------------------------------------------------ #
    def query_position(self) -> Optional[int]:
        """
        Read the position: 0..100, a limits-unset marker, or None when silent
        or refused. The subcommand is checked, so a delayed reply to a different
        query cannot be mistaken for a position.
        """
        resp = self._exchange(protocol.query_position(self.cfg.address))
        if isinstance(resp, protocol.QueryResponse) and resp.sub == protocol.QuerySub.POSITION:
            return resp.value
        return None

    def query_angle_raw(self) -> Optional[int]:
        """
        Read the raw slat angle byte; None when silent or refused (error reply).
        """
        resp = self._exchange(protocol.query_angle(self.cfg.address))
        if isinstance(resp, protocol.QueryResponse) and resp.sub == protocol.QuerySub.ANGLE:
            return resp.value
        return None

    # ------------------------------------------------------------------ #
    # address
    # ------------------------------------------------------------------ #
    def set_address(self, new_address: int) -> bool:
        """
        Change THIS device's address (unicast; refuses the broadcast address 0).

        Sent to the device's current address, so only this one device changes.
        The reply comes from the new address (spec 3.1). On success the runtime
        config follows the new address so control keeps working, and a warning is
        logged — the on-disk config must be updated to persist it.
        """
        return self._change_address(self.cfg.address, new_address, follow=True)

    def set_address_broadcast(self, new_address: int) -> bool:
        """
        Address EVERY motor on the bus at once (frame to the broadcast address).

        Safe only with a single motor connected. This actuator's own runtime
        address is deliberately not touched: the ack proves some motor took the
        address, not that it was this one — the config is the source of truth.
        """
        changed = self._change_address(protocol.BROADCAST_ADDRESS, new_address, follow=False)
        # Motors hold the bus after a broadcast flash write; waiting here (on
        # the bus-owning thread) keeps the next exchange from colliding.
        time.sleep(BROADCAST_HOLD_S)
        return changed

    def set_address_learning(self, new_address: int) -> bool:
        """
        Address the one motor whose button opened its ~1 min learning window.

        The only safe way to address motors on a shared bus; times out when no
        motor is in learning mode. The runtime address is not touched (see
        :meth:`set_address_broadcast`).
        """
        return self._change_address(protocol.LEARNING_ADDRESS, new_address, follow=False)

    def _change_address(self, target_address: int, new_address: int, follow: bool) -> bool:
        """
        Send a set-address frame to *target_address* and confirm the ack.

        *follow* (unicast only) makes the runtime config track the new address.
        """
        if new_address in (protocol.BROADCAST_ADDRESS, protocol.LEARNING_ADDRESS):
            raise ValueError(
                f"refusing to assign the reserved address 0x{new_address:02X} (broadcast/learning)"
            )
        if not 1 <= new_address <= 0xFE:
            raise ValueError("address must be 1..254")
        resp = self._exchange(
            protocol.set_address(target_address, new_address),
            # Spec 3.1: the ack comes FROM the new address. For broadcast/learning
            # a refusal may come from an arbitrary current address instead, so the
            # address filter is off there (the vendor driver does the same). A
            # timeout is the EXPECTED outcome of a learning write with no motor
            # in its window, so these ops do not count toward the offline
            # hysteresis of this actuator.
            expect_address=new_address if follow else ANY_ADDRESS,
            response_timeout_ms=FLASH_TIMEOUT_MS,
            total_timeout_ms=FLASH_TIMEOUT_MS,
            count_misses=follow,
        )
        if isinstance(resp, protocol.SetAddressResponse) and resp.ok:
            logger.warning(
                "%s: address write 0x%02X -> 0x%02X acknowledged; UPDATE THE CONFIG to persist it",
                self.cfg.device_id,
                target_address,
                new_address,
            )
            if follow:
                self.cfg.address = new_address  # follow at runtime so control keeps working
            return True
        logger.warning("%s: address change to 0x%02X failed/unconfirmed", self.cfg.device_id, new_address)
        return False

    # ------------------------------------------------------------------ #
    # liveness
    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        """
        Query the device address; update and return :attr:`online`.
        """
        self._exchange(protocol.query_address(self.cfg.address))
        return self.online

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _exchange(
        self,
        request: bytes,
        expect_address=None,
        response_timeout_ms=None,
        total_timeout_ms=None,
        count_misses=True,
    ):
        """
        Send *request*, validate and decode the reply, update ``online``.

        Returns the decoded response, or None on timeout / transport error / a
        frame that does not match the request (a stray or delayed frame from
        another device, or an unsolicited active report, on the shared bus).
        *count_misses* False keeps a failed exchange out of the offline
        hysteresis — for operations where no answer is an expected outcome.
        """
        if len(request) < 2:  # guard the request[1] access below
            raise ValueError("invalid request frame: too short (need at least address + function)")
        expect_address = self.cfg.address if expect_address is None else expect_address
        request_func = request[1]
        try:
            reply = self._t.transceive(
                self.cfg.port,
                request,
                RESP_SIZE,
                response_timeout_ms=response_timeout_ms,
                total_timeout_ms=total_timeout_ms,
            )
        except DeviceTimeout as exc:
            # A silent offline is hard to diagnose (a wrong address just looks
            # dead). Log it like a transport error so the reason is visible; this
            # repeats each poll while the device stays unreachable.
            logger.warning("%s: not responding: %s", self.cfg.device_id, exc)
            if count_misses:
                self._register_miss()
            return None
        except TransportError as exc:
            logger.warning("%s: transport error: %s", self.cfg.device_id, exc)
            if count_misses:
                self._register_miss()
            return None
        try:
            frame = protocol.parse_frame(reply)
        except protocol.ProtocolError as exc:
            logger.warning("%s: bad frame %s: %s", self.cfg.device_id, reply.hex(), exc)
            self._register_miss()
            return None
        # On a shared bus the reply must come from the addressed device and answer
        # the request we sent. A refusal (ERROR) may instead come from the device's
        # CURRENT address: a motor that rejects a set-address command never adopts
        # the new address, so its error report arrives from the old one. Anything
        # else — a stray/delayed reply from another device or an unsolicited active
        # report (0x08) — is not our answer, so count it as a miss (subject to
        # hysteresis) rather than trusting it.
        any_address = expect_address == ANY_ADDRESS
        is_reply = frame.function == request_func and (any_address or frame.address == expect_address)
        is_error_report = frame.function == protocol.Function.ERROR and (
            any_address or frame.address in (self.cfg.address, expect_address)
        )
        if not (is_reply or is_error_report):
            logger.debug(
                "%s: ignoring unexpected frame %s (addr 0x%02X func 0x%02X)",
                self.cfg.device_id,
                reply.hex(),
                frame.address,
                frame.function,
            )
            self._register_miss()
            return None
        resp = protocol.decode_response(frame)
        # The device answered (so it is online), but an error frame means it
        # rejected the command — surface it instead of silently ignoring it.
        if isinstance(resp, protocol.ErrorResponse):
            logger.warning("%s: device error response, code 0x%02X", self.cfg.device_id, resp.code)
        if not self.online:
            # visible at the default log level, symmetric to the offline warning
            logger.warning("%s: back online", self.cfg.device_id)
        self._miss_count = 0
        self.online = True
        return resp

    def _register_miss(self):
        """
        Count a missed exchange; go offline only after OFFLINE_AFTER_MISSES in a row.

        The counter is clamped at the threshold, so a long outage does not grow
        it unbounded; any successful exchange resets it to zero.
        """
        if self._miss_count < OFFLINE_AFTER_MISSES:
            self._miss_count += 1
        if self._miss_count >= OFFLINE_AFTER_MISSES and self.online:
            # Individual stray-frame misses are only logged at DEBUG, so the
            # transition itself must be visible at the default log level.
            logger.warning(
                "%s: offline after %d consecutive missed exchanges",
                self.cfg.device_id,
                self._miss_count,
            )
            self.online = False
