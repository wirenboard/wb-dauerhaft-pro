"""
Command layer: the prioritized TX queue.

MQTT command callbacks only enqueue; the daemon's main loop drains the queue
between polls, so all bus I/O stays on the single thread that owns the
half-duplex bus. Priorities are stop first, then movement, then the address
writes; a new movement replaces the queued one for the same device (only the
latest matters) and a stop cancels it outright.

The controls that feed this queue live in ``controls.py``.
"""

import logging
import threading
from collections import namedtuple

logger = logging.getLogger(__name__)

PRIO_STOP = 0
PRIO_MOVE = 1
PRIO_SETTING = 2

# One queued command: its priority, an insertion sequence (FIFO tie-break within
# a priority), a coalescing key (or None), and the zero-arg action to run.
_Entry = namedtuple("_Entry", "priority sequence key action")


class CommandQueue:
    """
    Priority queue of pending bus commands, drained between polls.

    ``ready`` is set on every :meth:`put`, so the poll loop can sleep on it
    instead of a fixed interval and a stop does not wait out the poll pause.
    """

    def __init__(self):
        self._items = []  # list of _Entry
        self._seq = 0
        self._lock = threading.Lock()  # put() runs on the MQTT thread, drain() on the bus thread
        self.ready = threading.Event()

    def put(self, priority, key, action):
        """
        Enqueue *action*; a queued entry with the same non-None *key* is replaced.
        """
        with self._lock:
            if key is not None:
                self._items = [entry for entry in self._items if entry.key != key]
            self._seq += 1
            self._items.append(_Entry(priority, self._seq, key, action))
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
                entry = min(self._items, key=lambda item: (item.priority, item.sequence))
                self._items.remove(entry)
            try:
                entry.action()
            except Exception:  # pylint: disable=broad-except
                # One failed command must not take the others (or the loop) down.
                logger.exception("queued command failed")
