"""
Command layer: the prioritized TX queue and the MQTT command controls.

MQTT callbacks only enqueue; the daemon's main loop drains the queue between
polls, so all bus I/O stays on the single thread that owns the half-duplex bus.
Priorities follow the vendor driver: stop first, then movement, then the
setting/address writes. A new movement command replaces the queued one for the
same device (only the latest matters) and a stop cancels it outright.

Control ids, titles and frames follow the agreed controls table; the
"Set Address To" field is input-only (it sends no frames, matching the vendor)
and the three address buttons next to it apply it as a unicast, broadcast or
button-learning write.
"""

import logging
import threading
import time

from . import protocol

logger = logging.getLogger(__name__)

PRIO_STOP = 0
PRIO_MOVE = 1
PRIO_SETTING = 2

# Motors ignore the bus for 0.5..1.2 s after a broadcast address write while
# they store it; hold our own polling off for the worst case (vendor value).
BROADCAST_HOLD_S = 1.2

# Human-readable texts for the position markers (values are not translated by
# the web UI, so they are English-only like every other published value).
_LIMIT_MARKERS = {
    protocol.POSITION_BOTH_LIMITS_UNSET: "limits not set",
    protocol.POSITION_LOWER_LIMIT_UNSET: "bottom limit not set",
    protocol.POSITION_UPPER_LIMIT_UNSET: "top limit not set",
}


class CommandQueue:
    """
    Priority queue of pending bus commands, drained between polls.

    ``ready`` is set on every :meth:`put`, so the poll loop can sleep on it
    instead of a fixed interval and a stop does not wait out the poll pause.
    """

    def __init__(self):
        self._items = []  # (priority, sequence, key, action)
        self._seq = 0
        self._lock = threading.Lock()  # put() runs on the MQTT thread, drain() on the bus thread
        self.ready = threading.Event()

    def put(self, priority, key, action):
        """
        Enqueue *action*; a queued entry with the same non-None *key* is replaced.
        """
        with self._lock:
            if key is not None:
                self._items = [item for item in self._items if item[2] != key]
            self._seq += 1
            self._items.append((priority, self._seq, key, action))
        self.ready.set()

    def drain(self):
        """
        Run the queued actions in priority order (FIFO within one priority).
        """
        self.ready.clear()  # before running, so a put() during a command re-arms it
        with self._lock:
            items, self._items = sorted(self._items, key=lambda item: item[:2]), []
        for _priority, _seq, _key, action in items:
            try:
                action()
            except Exception:  # pylint: disable=broad-except
                # One failed command must not take the others (or the loop) down.
                logger.exception("queued command failed")


class ActuatorControls:
    """
    The command controls of one actuator: creation, callbacks and telemetry.

    ``reverse`` only remaps the UI (swapped open/close, mirrored position); it
    is runtime-only and never reaches the wire — not to be confused with the
    protocol's change-direction setting, which this driver does not send.
    """

    def __init__(self, dev, act, queue):
        self._dev = dev
        self._act = act
        self._queue = queue
        self._reverse = False
        self._addr_target = act.cfg.address  # last value of the input field
        self._move_key = ("move", act.cfg.device_id)
        self._compressed = act.cfg.slat_angle_mode == "compressed"

    def create(self):
        """
        Publish the command controls and subscribe to their command topics.

        One row per control: (name, type, order, ru title, en title, handler,
        extra add_control kwargs). Order 5 is the read-only address indicator,
        created by the daemon. Pushbuttons get no retained value (initial None).
        """
        button = {"initial": None}
        rows = [
            ("up", "pushbutton", 1, "Открыть", "Open", self._on_up, button),
            ("stop", "pushbutton", 2, "Стоп", "Stop", self._on_stop, button),
            ("down", "pushbutton", 3, "Закрыть", "Close", self._on_down, button),
            ("position_current", "text", 4, "Текущая позиция", "Current Position", None, {"readonly": True}),
            (
                "set_address",
                "value",
                6,
                "Сменить адрес на",
                "Set Address To",
                self._on_addr_target,
                {"min_value": 1, "max_value": 255, "initial": self._addr_target},
            ),
            ("address_set", "pushbutton", 7, "Сменить адрес", "Set Address", self._on_addr_set, button),
            (
                "address_set_broadcast",
                "pushbutton",
                8,
                "Установить адрес (broadcast)",
                "Set Address (broadcast)",
                self._on_addr_broadcast,
                button,
            ),
            (
                "address_set_learning",
                "pushbutton",
                9,
                "Установить адрес (learning)",
                "Set Address (learning)",
                self._on_addr_learning,
                button,
            ),
            ("reverse", "switch", 10, "Реверс", "Reverse", self._on_reverse, {"initial": 0}),
            (
                "point3_set",
                "pushbutton",
                11,
                "Установить промежуточную точку",
                "Set a Waypoint",
                self._on_point3_set,
                button,
            ),
            (
                "point3_go",
                "pushbutton",
                12,
                "Перейти на промежуточную точку",
                "Go to a Waypoint",
                self._on_point3_go,
                button,
            ),
        ]
        if self._act.cfg.slat_angle_mode != "none":
            angle_extra = {"min_value": 0, "max_value": protocol.ANGLE_MAX, "initial": None}
            rows.append(
                ("slat_angle", "range", 13, "Угол ламелей", "Slat Angle", self._on_slat_angle, angle_extra)
            )
            rows.append(
                (
                    "slat_angle_current",
                    "text",
                    14,
                    "Текущий угол ламелей",
                    "Current Slat Angle",
                    None,
                    {"readonly": True},
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
        pos = self._act.query_position()
        if pos is not None:
            text = _LIMIT_MARKERS.get(pos)
            if text is None:
                text = str(100 - pos if self._reverse and pos <= 100 else pos)
            self._dev.set_value("position_current", text)
        if self._act.cfg.slat_angle_mode == "none":
            return
        raw = self._act.query_angle_raw()
        if raw is not None:
            degrees = protocol.raw_to_angle(raw, self._compressed)
            self._dev.set_value("slat_angle_current", str(degrees))
            self._dev.set_value("slat_angle", str(degrees))

    # ------------------------------------------------------------------ #
    # command callbacks (paho signature: client, userdata, message)
    # ------------------------------------------------------------------ #
    def _on_up(self, *_):
        self._queue.put(PRIO_MOVE, self._move_key, self._act.down if self._reverse else self._act.up)

    def _on_down(self, *_):
        self._queue.put(PRIO_MOVE, self._move_key, self._act.up if self._reverse else self._act.down)

    def _on_stop(self, *_):
        # Highest priority; the shared key also cancels any queued movement.
        self._queue.put(PRIO_STOP, self._move_key, self._act.stop)

    def _on_point3_go(self, *_):
        self._queue.put(PRIO_MOVE, self._move_key, self._act.go_third_point)

    def _on_point3_set(self, *_):
        self._queue.put(PRIO_SETTING, None, self._act.set_third_point)

    def _on_reverse(self, _client, _userdata, msg):
        self._reverse = msg.payload == b"1"
        self._dev.set_value("reverse", 1 if self._reverse else 0)

    def _on_slat_angle(self, _client, _userdata, msg):
        degrees = self._int_payload(msg)
        if degrees is None or not 0 <= degrees <= protocol.ANGLE_MAX:
            return
        raw = protocol.angle_to_raw(degrees, self._compressed)

        def action():
            self._act.set_angle_raw(raw)
            self._dev.set_value("slat_angle", degrees)

        self._queue.put(PRIO_MOVE, self._move_key, action)

    def _on_addr_target(self, _client, _userdata, msg):
        # Input field only — remembered and echoed back, no frames sent (the
        # address buttons below apply it), matching the vendor panel.
        target = self._int_payload(msg)
        if target is None or not 1 <= target <= 255:
            return
        self._addr_target = target
        self._dev.set_value("set_address", target)

    def _enqueue_address_write(self, write, hold_s=0.0):
        target = self._addr_target

        def action():
            write(target)
            if hold_s:
                # Motors hold the bus after a broadcast flash write; waiting here
                # (on the bus-owning thread) keeps the next poll from colliding.
                time.sleep(hold_s)

        self._queue.put(PRIO_SETTING, None, action)

    def _on_addr_set(self, *_):
        self._enqueue_address_write(self._act.set_address)

    def _on_addr_broadcast(self, *_):
        self._enqueue_address_write(self._act.set_address_broadcast, hold_s=BROADCAST_HOLD_S)

    def _on_addr_learning(self, *_):
        self._enqueue_address_write(self._act.set_address_learning)

    @staticmethod
    def _int_payload(msg):
        """
        Decode an integer command payload; None (and a log line) when malformed.
        """
        try:
            return int(msg.payload.decode())
        except (UnicodeDecodeError, ValueError):
            logger.warning("ignoring malformed command payload %r on %s", msg.payload, msg.topic)
            return None
