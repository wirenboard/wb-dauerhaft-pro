"""Actuator model: maps the MVP actions to protocol frames and tracks liveness.

One :class:`Actuator` per physical device. It owns no MQTT and no I/O loop; it
uses an injected transport (:class:`~wb_mqtt_dauerhaft_pro.transport.SerialTransport`
or a test fake) to talk to the device.

MVP scope — nothing else:
  * :meth:`up` / :meth:`down` / :meth:`stop` — drive the motor;
  * :meth:`set_address` — change the device's RS-485 address;
  * :meth:`ping` — read the device address, used as a liveness probe.
"""

import logging
from dataclasses import dataclass

from . import protocol
from .transport import DeviceTimeout, PortConfig, TransportError

logger = logging.getLogger(__name__)

# A reply is addr+func+len + up to 2 data bytes + 2 CRC = 7 bytes for every MVP
# command (move/stop echo, address query, set-address ack).
RESP_SIZE = 7

# Changing the address writes the device's flash and answers slower than a plain
# move; the vendor driver allows several seconds for such operations.
ADDRESS_TIMEOUT_MS = 5000


@dataclass
class ActuatorConfig:
    mqtt_id: str
    title: str
    address: int
    port: PortConfig


class Actuator:
    """High-level control + liveness for one Dauerhaft PRO actuator (MVP)."""

    def __init__(self, config: ActuatorConfig, transport):
        self.cfg = config
        self._t = transport
        self.online = False

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
            total_timeout_ms=ADDRESS_TIMEOUT_MS,
        )
        if isinstance(resp, protocol.SetAddressResponse) and resp.ok:
            logger.warning(
                "%s: address changed 0x%02X -> 0x%02X; UPDATE THE CONFIG to persist it",
                self.cfg.mqtt_id, self.cfg.address, new_address,
            )
            self.cfg.address = new_address  # follow at runtime so control keeps working
            return True
        logger.warning("%s: address change to 0x%02X failed/unconfirmed",
                       self.cfg.mqtt_id, new_address)
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
    def _exchange(self, request: bytes, total_timeout_ms=None):
        """Send *request*, decode the reply, update ``online``; return response or None."""
        try:
            reply = self._t.transceive(self.cfg.port, request, RESP_SIZE,
                                       total_timeout_ms=total_timeout_ms)
        except DeviceTimeout:
            self.online = False
            return None
        except TransportError as exc:
            logger.warning("%s: transport error: %s", self.cfg.mqtt_id, exc)
            self.online = False
            return None
        try:
            resp = protocol.decode_response(protocol.parse_frame(reply))
        except protocol.ProtocolError as exc:
            logger.warning("%s: bad frame %s: %s", self.cfg.mqtt_id, reply.hex(), exc)
            self.online = False
            return None
        self.online = True
        return resp
