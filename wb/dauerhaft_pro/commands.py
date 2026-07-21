"""
Command layer: the prioritized TX queue and the MQTT command controls.

MQTT callbacks only enqueue; the daemon's main loop drains the queue between
polls, so all bus I/O stays on the single thread that owns the half-duplex bus.
Priorities are stop first, then movement, then the address writes. A new
movement command replaces the queued one for the same device (only the latest
matters) and a stop cancels it outright.

Control ids and titles follow the agreed controls table. The "New Address"
field is input-only (it sends no frames); the "Set New Address" button next to
it applies the entered value as a unicast address change.
"""

import logging
import threading

logger = logging.getLogger(__name__)

PRIO_STOP = 0
PRIO_MOVE = 1
PRIO_SETTING = 2


def _ignore_retained(handler):
    """
    Wrap a command handler to drop retained messages.

    A command retained on the broker would otherwise replay on every daemon
    restart — a control the user never actually pressed.
    """

    def wrapped(client, userdata, msg):
        if msg.retain:
            logger.warning("ignoring retained command on %s", msg.topic)
            return
        handler(client, userdata, msg)

    return wrapped


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

        Actions are picked one at a time, so a stop arriving while a slow write
        runs is executed right after it — not after every queued write.
        """
        self.ready.clear()  # before running, so a put() during a command re-arms it
        while True:
            with self._lock:
                if not self._items:
                    return
                item = min(self._items, key=lambda entry: entry[:2])
                self._items.remove(item)
            try:
                item[3]()
            except Exception:  # pylint: disable=broad-except
                # One failed command must not take the others (or the loop) down.
                logger.exception("queued command failed")


class ActuatorControls:
    """
    The command controls of one actuator: creation and command callbacks.
    """

    def __init__(self, dev, actuator, queue):
        self._dev = dev
        self._actuator = actuator
        self._queue = queue
        self._addr_target = actuator.cfg.address  # last value of the input field
        self._move_key = ("move", actuator.cfg.device_id)

    def create(self):
        """
        Publish the command controls and subscribe to their command topics.

        One row per control: (name, type, order, ru title, en title, handler,
        extra add_control kwargs). Orders leave gaps for controls added later:
        order 4 (position) and order 5 (the daemon's address indicator).
        Pushbuttons get no retained value (initial None).
        """
        button = {"initial": None}
        rows = [
            ("up", "pushbutton", 1, "Открыть", "Open", self._on_up, button),
            ("stop", "pushbutton", 2, "Стоп", "Stop", self._on_stop, button),
            ("down", "pushbutton", 3, "Закрыть", "Close", self._on_down, button),
            (
                "set_address",
                "value",
                6,
                "Новый адрес",
                "New Address",
                self._on_addr_target,
                {"min_value": 1, "max_value": 255, "initial": self._addr_target},
            ),
            (
                "address_set",
                "pushbutton",
                7,
                "Установить новый адрес",
                "Set New Address",
                self._on_addr_set,
                button,
            ),
        ]
        for name, control_type, order, ru_title, en_title, handler, extra in rows:
            self._dev.add_control(name, control_type, order, title={"ru": ru_title, "en": en_title}, **extra)
            if handler is not None:
                self._dev.on_command(name, _ignore_retained(handler))

    # ------------------------------------------------------------------ #
    # command callbacks (paho signature: client, userdata, message)
    # ------------------------------------------------------------------ #
    def _on_up(self, *_):
        self._queue.put(PRIO_MOVE, self._move_key, self._actuator.up)

    def _on_down(self, *_):
        self._queue.put(PRIO_MOVE, self._move_key, self._actuator.down)

    def _on_stop(self, *_):
        # Highest priority; the shared key also cancels any queued movement.
        self._queue.put(PRIO_STOP, self._move_key, self._actuator.stop)

    def _on_addr_target(self, _client, _userdata, msg):
        # Input field only — remembered and echoed back, no frames sent; the
        # "Set New Address" button applies it.
        target = self._parse_int_payload(msg)
        if target is None or not 1 <= target <= 255:
            return
        self._addr_target = target
        self._dev.set_value("set_address", target)

    def _on_addr_set(self, *_):
        target = self._addr_target
        self._queue.put(PRIO_SETTING, None, lambda: self._actuator.set_address(target))

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
