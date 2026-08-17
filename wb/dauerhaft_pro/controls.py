"""
Device controls: the control table of one actuator and its state publishing.

Single source of truth for every MQTT control of a device — the read-only
position and address indicators, the motion / waypoint / slat-angle controls and
the address change — with their display order and bilingual titles.

The command callbacks only enqueue onto the shared CommandQueue; the daemon's
poll loop drains it, so all bus I/O stays on the one thread that owns the bus.
The "New Address" field is input-only (it sends no frames); the "Set New
Address" button applies the entered value, using the method chosen by the
config's learning_type. The slat-angle controls exist only for slat/lamella
curtains (config slat_angle_mode other than "none"). Reverse is a config
setting (ActuatorConfig.reverse), applied at startup — it has no widget control.
"""

import logging

from . import protocol
from .commands import PRIO_MOVE, PRIO_SETTING, PRIO_STOP

logger = logging.getLogger(__name__)

# Control display order (single source of truth; the UI sorts controls by it).
ORDER_OPEN = 1
ORDER_STOP = 2
ORDER_CLOSE = 3
ORDER_POSITION = 4
ORDER_ADDRESS = 5
ORDER_NEW_ADDRESS = 6
ORDER_APPLY_ADDRESS = 7
ORDER_WAYPOINT_SET = 8
ORDER_WAYPOINT_GO = 9
ORDER_SLAT_ANGLE = 10
ORDER_SLAT_ANGLE_CURRENT = 11

# Whether the wire scale is compressed, per the config's slat_angle_mode. A
# lookup instead of string comparisons: an unknown mode fails at startup here,
# whatever the config loader let through.
_SCALE_COMPRESSED = {"none": False, "direct": False, "compressed": True}

# Human-readable position markers. Control VALUES are not translated by the web
# UI, so — unlike code text, which stays English — these are Russian to match
# the panel language.
_LIMIT_MARKERS = {
    protocol.POSITION_BOTH_LIMITS_UNSET: "пределы не заданы",
    protocol.POSITION_LOWER_LIMIT_UNSET: "нижний предел не задан",
    protocol.POSITION_UPPER_LIMIT_UNSET: "верхний предел не задан",
}


def _fmt_address(address: int) -> str:
    """
    Format an RS-485 address as a decimal string for the read-only indicator.

    Decimal to match the "New Address" input field, so the shown address and a
    value being entered are directly comparable. The control's initial value and
    every poll update must format identically, or the retained-dedup would
    republish the address on every cycle.
    """
    return str(address)


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
    Every MQTT control of one actuator: creation, callbacks and telemetry.

    ``reverse`` (config-only, see ActuatorConfig.reverse) remaps the UI at
    startup — swapped open/close and a mirrored position; it never reaches the wire.
    """

    def __init__(self, dev, actuator, queue):
        self._dev = dev
        self._actuator = actuator
        self._queue = queue
        self._reverse = actuator.cfg.reverse  # from the config (json-editor); no runtime widget toggle
        self._addr_target = actuator.cfg.address  # last value of the input field
        self._move_key = ("move", actuator.cfg.device_id)
        self._addr_key = ("addr", actuator.cfg.device_id)
        self._waypoint_key = ("waypoint", actuator.cfg.device_id)
        try:
            self._compressed = _SCALE_COMPRESSED[actuator.cfg.slat_angle_mode]
        except KeyError:
            raise ValueError(
                f"{actuator.cfg.device_id}: unknown slat_angle_mode {actuator.cfg.slat_angle_mode!r}"
            ) from None

    def create(self):
        """
        Publish every control of the device and subscribe the command topics.

        One row per control: (name, type, order, ru title, en title, handler,
        extra add_control kwargs). Read-only indicators (position, address,
        current slat angle) have no handler; pushbuttons carry no retained value
        (initial None). The slat-angle controls are added only when the config
        enables them (slat_angle_mode other than "none").
        """
        readonly = {"readonly": True}
        button = {"initial": None}
        rows = [
            ("up", "pushbutton", ORDER_OPEN, "Открыть", "Open", self._on_up, button),
            ("stop", "pushbutton", ORDER_STOP, "Стоп", "Stop", self._on_stop, button),
            ("down", "pushbutton", ORDER_CLOSE, "Закрыть", "Close", self._on_down, button),
            ("position_current", "text", ORDER_POSITION, "Позиция", "Position", None, readonly),
            (
                "address",
                "text",
                ORDER_ADDRESS,
                "Адрес",
                "Address",
                None,
                {"readonly": True, "initial": _fmt_address(self._actuator.cfg.address)},
            ),
            (
                "set_address",
                "value",
                ORDER_NEW_ADDRESS,
                "Новый адрес",
                "New Address",
                self._on_addr_target,
                {"min_value": 1, "max_value": protocol.MAX_DEVICE_ADDRESS, "initial": self._addr_target},
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
            (
                "point3_set",
                "pushbutton",
                ORDER_WAYPOINT_SET,
                "Установить промежуточную точку",
                "Set a Waypoint",
                self._on_point3_set,
                button,
            ),
            (
                "point3_go",
                "pushbutton",
                ORDER_WAYPOINT_GO,
                "Перейти на промежуточную точку",
                "Go to a Waypoint",
                self._on_point3_go,
                button,
            ),
        ]
        if self._actuator.cfg.slat_angle_mode != "none":
            rows.append(
                (
                    "slat_angle",
                    "range",
                    ORDER_SLAT_ANGLE,
                    "Угол ламелей, °",
                    "Slat Angle, °",
                    self._on_slat_angle,
                    {"min_value": 0, "max_value": protocol.ANGLE_MAX, "initial": None},
                )
            )
            rows.append(
                (
                    "slat_angle_current",
                    "text",
                    ORDER_SLAT_ANGLE_CURRENT,
                    "Текущий угол ламелей",
                    "Current Slat Angle",
                    None,
                    readonly,
                )
            )
        for name, control_type, order, ru_title, en_title, handler, extra in rows:
            self._dev.add_control(name, control_type, order, title={"ru": ru_title, "en": en_title}, **extra)
            if handler is not None:
                self._dev.on_command(name, handler)

    def publish_telemetry(self):
        """
        Poll and publish the position (and slat angle when enabled).

        Meant to be called while the device is online; a silent device simply
        keeps its last published state.
        """
        pos = self._actuator.query_position()
        if pos is not None:
            text = _LIMIT_MARKERS.get(pos)
            if text is None:
                shown = 100 - pos if self._reverse and pos <= 100 else pos
                text = f"{shown} %"
            self._dev.set_value("position_current", text)
        if self._actuator.cfg.slat_angle_mode == "none":
            return
        raw = self._actuator.query_angle_raw()
        if raw is not None:
            # Clamp: a raw byte outside the scale (e.g. a marker) must not push
            # an out-of-range value into the 0..180 range control.
            degrees = max(0, min(protocol.ANGLE_MAX, protocol.raw_to_angle(raw, self._compressed)))
            # Only the read-only indicator — writing the live angle into the
            # slat_angle range would fight the user's setpoint while the motor moves.
            self._dev.set_value("slat_angle_current", f"{degrees} °")

    # ------------------------------------------------------------------ #
    # command callbacks (paho signature: client, userdata, message)
    # ------------------------------------------------------------------ #
    def _on_up(self, *_):
        """
        Queue an open command (movement priority); reverse swaps open/close.
        """
        action = self._actuator.down if self._reverse else self._actuator.up
        self._queue.put(PRIO_MOVE, self._move_key, action)

    def _on_down(self, *_):
        """
        Queue a close command (movement priority); reverse swaps open/close.
        """
        action = self._actuator.up if self._reverse else self._actuator.down
        self._queue.put(PRIO_MOVE, self._move_key, action)

    def _on_stop(self, *_):
        """
        Queue a stop (top priority); the shared move key also cancels a queued
        movement.
        """
        self._queue.put(PRIO_STOP, self._move_key, self._actuator.stop)

    def _on_point3_set(self, *_):
        """
        Queue storing the current position as the waypoint (setting priority).

        Keyed so repeated presses coalesce to a single flash write.
        """
        self._queue.put(PRIO_SETTING, self._waypoint_key, self._actuator.set_third_point)

    def _on_point3_go(self, *_):
        """
        Queue driving to the stored waypoint (movement priority).
        """
        self._queue.put(PRIO_MOVE, self._move_key, self._actuator.go_third_point)

    def _on_slat_angle(self, _client, _userdata, msg):
        """
        Queue rotating the slats to the requested angle (movement priority).
        """
        degrees = self._parse_int_payload(msg)
        if degrees is None or not 0 <= degrees <= protocol.ANGLE_MAX:
            return
        raw = protocol.angle_to_raw(degrees, self._compressed)

        def action():
            self._actuator.set_angle_raw(raw)
            self._dev.set_value("slat_angle", degrees)

        self._queue.put(PRIO_MOVE, self._move_key, action)

    def _on_addr_target(self, _client, _userdata, msg):
        """
        Remember the New Address field value; input only, sends no frames — the
        Set New Address button applies it.
        """
        target = self._parse_int_payload(msg)
        if target is None or not 1 <= target <= protocol.MAX_DEVICE_ADDRESS:
            return
        self._addr_target = target
        self._dev.set_value("set_address", target)

    def _on_addr_set(self, *_):
        """
        Queue applying the remembered address using the configured method.

        ``physical_button`` addresses the motor through its learning window (the
        user presses the motor's button); any other learning_type retargets the
        motor directly by unicast. Keyed so repeated presses coalesce to the
        latest target instead of queuing several flash writes.
        """
        target = self._addr_target
        if self._actuator.cfg.learning_type == "physical_button":
            write = self._actuator.set_address_learning
        else:
            write = self._actuator.set_address
        self._queue.put(PRIO_SETTING, self._addr_key, lambda: write(target))

    def _parse_int_payload(self, msg):
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
        logger.warning(
            "%s: ignoring malformed command payload %r on %s", self._dev.id, msg.payload[:32], msg.topic
        )
        return None
