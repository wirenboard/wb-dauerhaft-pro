"""
Device controls: the control table of one actuator and its state publishing.

Single source of truth for every MQTT control of a device — the read-only
address indicator and the command controls (open / stop / close, plus a
unicast address change) — with their display order and bilingual titles.

The command callbacks only enqueue onto the shared CommandQueue; the daemon's
poll loop drains it, so all bus I/O stays on the one thread that owns the bus.
The "New Address" field is input-only (it sends no frames); the "Set New
Address" button applies the entered value as a unicast address change.
"""

import logging

from .commands import PRIO_MOVE, PRIO_SETTING, PRIO_STOP, _ignore_retained

logger = logging.getLogger(__name__)

# Control display order (single source of truth; the UI sorts controls by it).
# The gap at 4 is reserved for the position indicator added later.
ORDER_OPEN = 1
ORDER_STOP = 2
ORDER_CLOSE = 3
ORDER_ADDRESS = 5
ORDER_NEW_ADDRESS = 6
ORDER_APPLY_ADDRESS = 7


def _fmt_address(address: int) -> str:
    """
    Format an RS-485 address as the driver's canonical ``0xHH`` string.

    Used for both the retained ``address`` control value and the startup log;
    the control's initial value and every poll update must format identically,
    or the retained-dedup would republish the address on every cycle.
    """
    return f"0x{address:02X}"


def publish_state(dev, actuator) -> None:
    """
    Publish the actuator's availability and its current address.

    Availability follows the WB convention: an empty ``/meta/error`` means OK,
    a non-empty value ("r") marks the device unavailable. Deduplicated retained
    publishing keeps unchanged states quiet.
    """
    dev.set_error("" if actuator.online else "r")
    dev.set_value("address", _fmt_address(actuator.cfg.address))


class DeviceControls:
    """
    Every MQTT control of one actuator: creation, command callbacks and keys.
    """

    def __init__(self, dev, actuator, queue):
        self._dev = dev
        self._actuator = actuator
        self._queue = queue
        self._addr_target = actuator.cfg.address  # last value of the input field
        self._move_key = ("move", actuator.cfg.device_id)
        self._addr_key = ("addr", actuator.cfg.device_id)

    def create(self):
        """
        Publish every control of the device and subscribe the command topics.

        The read-only address indicator plus the command controls, each with its
        display order (see the ORDER_* constants). Pushbuttons carry no retained
        value (initial None).
        """
        self._dev.add_control(
            "address",
            "text",
            ORDER_ADDRESS,
            readonly=True,
            title={"ru": "Адрес", "en": "Address"},
            initial=_fmt_address(self._actuator.cfg.address),
        )
        button = {"initial": None}
        rows = [
            ("up", "pushbutton", ORDER_OPEN, "Открыть", "Open", self._on_up, button),
            ("stop", "pushbutton", ORDER_STOP, "Стоп", "Stop", self._on_stop, button),
            ("down", "pushbutton", ORDER_CLOSE, "Закрыть", "Close", self._on_down, button),
            (
                "set_address",
                "value",
                ORDER_NEW_ADDRESS,
                "Новый адрес",
                "New Address",
                self._on_addr_target,
                {"min_value": 1, "max_value": 255, "initial": self._addr_target},
            ),
            (
                "address_set",
                "pushbutton",
                ORDER_APPLY_ADDRESS,
                "Установить новый адрес",
                "Set New Address",
                self._on_addr_set,
                button,
            ),
        ]
        for name, control_type, order, ru_title, en_title, handler, extra in rows:
            self._dev.add_control(name, control_type, order, title={"ru": ru_title, "en": en_title}, **extra)
            self._dev.on_command(name, _ignore_retained(handler))

    # ------------------------------------------------------------------ #
    # command callbacks (paho signature: client, userdata, message)
    # ------------------------------------------------------------------ #
    def _on_up(self, *_):
        """
        Queue an open command (movement priority).
        """
        self._queue.put(PRIO_MOVE, self._move_key, self._actuator.up)

    def _on_down(self, *_):
        """
        Queue a close command (movement priority).
        """
        self._queue.put(PRIO_MOVE, self._move_key, self._actuator.down)

    def _on_stop(self, *_):
        """
        Queue a stop (top priority); the shared move key also cancels a queued
        movement.
        """
        self._queue.put(PRIO_STOP, self._move_key, self._actuator.stop)

    def _on_addr_target(self, _client, _userdata, msg):
        """
        Remember the New Address field value; input only, sends no frames — the
        Set New Address button applies it.
        """
        target = self._parse_int_payload(msg)
        if target is None or not 1 <= target <= 255:
            return
        self._addr_target = target
        self._dev.set_value("set_address", target)

    def _on_addr_set(self, *_):
        """
        Queue applying the remembered address as a unicast change.

        Keyed like movement so repeated presses coalesce to the latest target
        instead of queuing several flash writes.
        """
        target = self._addr_target
        self._queue.put(PRIO_SETTING, self._addr_key, lambda: self._actuator.set_address(target))

    @staticmethod
    def _parse_int_payload(msg):
        """
        Decode an integer command payload; None (and a log line) when malformed.

        The length cap keeps a huge payload from tying up the MQTT thread in
        int() and from being dumped into the log.
        """
        if len(msg.payload) <= 8:
            try:
                return int(msg.payload.decode())
            except (UnicodeDecodeError, ValueError):
                pass
        logger.warning("ignoring malformed command payload %r on %s", msg.payload[:32], msg.topic)
        return None
