"""
Command layer: the TX queue (priority, replace-by-key, ready arming).
"""

from wb.dauerhaft_pro.commands import PRIO_MOVE, PRIO_SETTING, PRIO_STOP, CommandQueue


def test_stop_cancels_queued_movement_and_runs_first():
    """
    Stop runs first and cancels a queued movement that shares its key.
    """
    queue, ran = CommandQueue(), []
    queue.put(PRIO_SETTING, None, lambda: ran.append("setting"))
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("move"))
    queue.put(PRIO_STOP, "move-a", lambda: ran.append("stop"))
    queue.drain()
    assert ran == ["stop", "setting"]  # the queued movement was replaced by its key


def test_new_movement_replaces_the_queued_one():
    """
    A new movement replaces the queued one for the same key; other keys stay.
    """
    queue, ran = CommandQueue(), []
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("up"))
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("down"))
    queue.put(PRIO_MOVE, "move-b", lambda: ran.append("other"))
    queue.drain()
    assert ran == ["down", "other"]


def test_ready_reflects_pending_work():
    """
    put() arms ``ready`` (the poll loop waits on it); drain() clears it, and a
    put() during a command re-arms it so nothing is missed.
    """
    queue = CommandQueue()
    assert not queue.ready.is_set()
    queue.put(PRIO_MOVE, "k", lambda: None)
    assert queue.ready.is_set()  # armed for the loop to wake
    queue.drain()
    assert not queue.ready.is_set()  # cleared once drained
    queue.put(PRIO_MOVE, "k", lambda: queue.put(PRIO_STOP, None, lambda: None))
    queue.drain()
    assert queue.ready.is_set()  # re-armed by the nested put
