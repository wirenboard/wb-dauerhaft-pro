"""Actuator model: maps the supported actions to protocol frames and tracks liveness.

One :class:`Actuator` per physical device. It owns no MQTT and no I/O loop; it
uses an injected transport (:class:`~wb_dauerhaft_pro.transport.SerialTransport`
or a test fake) to talk to the device.

Supported operations:
  * :meth:`up` / :meth:`down` / :meth:`stop` — drive the motor;
  * :meth:`set_address` — change the device's RS-485 address;
  * :meth:`ping` — read the device address, used as a liveness probe.
"""

import logging
from dataclasses import dataclass

from . import protocol
from .transport import DeviceTimeout, PortConfig, TransportError

logger = logging.getLogger(__name__)

# A reply is addr+func+len + up to 2 data bytes + 2 CRC = 7 bytes for every
# supported command (move/stop echo, address query, set-address ack). This is
# exact only because these commands have no variable-length replies; a
# firmware-version query (3 data bytes) or similar would need its own size,
# derived from the request.
RESP_SIZE = 7

# Changing the address writes the device's flash and answers slower than a plain
# move; the vendor driver allows several seconds for such operations.
ADDRESS_TIMEOUT_MS = 5000

# Mark a device offline only after this many consecutive missed exchanges, so a
# single transient RS-485 glitch — e.g. a collision with a chatty device's active
# report on a shared bus — does not flap the availability indicator.
OFFLINE_AFTER_MISSES = 3


@dataclass
class ActuatorConfig:
    mqtt_id: str
    title: str
    address: int
    port: PortConfig


class Actuator:
    """High-level control + liveness for one Dauerhaft PRO actuator."""

    def __init__(self, config: ActuatorConfig, transport):
        self.cfg = config
        self._t = transport
        self.online = False
        self._miss_count = 0

    # ------------------------------------------------------------------ #
    # motion
    # ------------------------------------------------------------------ #
    def up(self):
        """Drive up / open."""
        self._exchange(protocol.control_up(self.cfg.address))

    def down(self):
        """Drive down / close."""
        self._exchange(protocol.control_down(self.cfg.address))

    def stop(self):
        """Stop motion."""
        self._exchange(protocol.control_stop(self.cfg.address))

    # ------------------------------------------------------------------ #
    # address
    # ------------------------------------------------------------------ #
    def set_address(self, new_address: int) -> bool:
        """Change THIS device's address (unicast; refuses the broadcast address 0).

        Sent to the device's current address, so only this one device changes.
        The reply comes from the new address (spec 3.1). On success the runtime
        config follows the new address so control keeps working, and a warning is
        logged — the on-disk config must be updated to persist it.
        """
        if new_address == protocol.BROADCAST_ADDRESS:
            raise ValueError("refusing to assign address 0 (broadcast/universal)")
        if not 1 <= new_address <= 0xFF:
            raise ValueError("address must be 1..255")

        resp = self._exchange(
            protocol.set_address(self.cfg.address, new_address),
            expect_address=new_address,  # spec 3.1: the ack comes FROM the new address
            response_timeout_ms=ADDRESS_TIMEOUT_MS,
            total_timeout_ms=ADDRESS_TIMEOUT_MS,
        )
        if isinstance(resp, protocol.SetAddressResponse) and resp.ok:
            logger.warning(
                "%s: address changed 0x%02X -> 0x%02X; UPDATE THE CONFIG to persist it",
                self.cfg.mqtt_id,
                self.cfg.address,
                new_address,
            )
            self.cfg.address = new_address  # follow at runtime so control keeps working
            return True
        logger.warning("%s: address change to 0x%02X failed/unconfirmed", self.cfg.mqtt_id, new_address)
        return False

    # ------------------------------------------------------------------ #
    # liveness
    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        """Query the device address; update and return :attr:`online`."""
        resp = self._exchange(protocol.query_address(self.cfg.address))
        return self.online

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _exchange(self, request: bytes, expect_address=None, response_timeout_ms=None, total_timeout_ms=None):
        """Send *request*, validate and decode the reply, update ``online``.

        Returns the decoded response, or None on timeout / transport error / a
        frame that does not match the request (a stray or delayed frame from
        another device, or an unsolicited active report, on the shared bus).
        """
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
            logger.warning("%s: not responding: %s", self.cfg.mqtt_id, exc)
            self._register_miss()
            return None
        except TransportError as exc:
            logger.warning("%s: transport error: %s", self.cfg.mqtt_id, exc)
            self._register_miss()
            return None
        try:
            frame = protocol.parse_frame(reply)
        except protocol.ProtocolError as exc:
            logger.warning("%s: bad frame %s: %s", self.cfg.mqtt_id, reply.hex(), exc)
            self._register_miss()
            return None
        # On a shared bus the reply must come from the addressed device and answer
        # the request we sent (or be an error report). A mismatched frame — a
        # stray/delayed reply from another device or an unsolicited active report
        # (0x08) — is not our answer, so count it as a miss (subject to hysteresis)
        # rather than trusting it.
        if frame.address != expect_address or frame.function not in (request_func, protocol.Function.ERROR):
            logger.debug(
                "%s: ignoring unexpected frame %s (addr 0x%02X func 0x%02X)",
                self.cfg.mqtt_id,
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
            logger.warning("%s: device error response, code 0x%02X", self.cfg.mqtt_id, resp.code)
        self._miss_count = 0
        self.online = True
        return resp

    def _register_miss(self):
        """Count a missed exchange; go offline only after OFFLINE_AFTER_MISSES in a row."""
        self._miss_count += 1
        if self._miss_count >= OFFLINE_AFTER_MISSES:
            self.online = False
