"""
Command layer: the TX queue's priority/replace semantics and the telemetry
transformations of the actuator controls.
"""

from types import SimpleNamespace

from wb.dauerhaft_pro import protocol
from wb.dauerhaft_pro.commands import (
    PRIO_MOVE,
    PRIO_SETTING,
    PRIO_STOP,
    ActuatorControls,
    CommandQueue,
)


class RecordingDevice:
    def __init__(self):
        self.values = {}

    def set_value(self, name, value):
        self.values[name] = value


class FakeActuator:
    def __init__(self, position, angle_raw=None, slat_angle_mode="none"):
        self.cfg = SimpleNamespace(device_id="a", address=0x0B, slat_angle_mode=slat_angle_mode)
        self.position = position
        self.angle_raw = angle_raw

    def query_position(self):
        return self.position

    def query_angle_raw(self):
        return self.angle_raw


def test_stop_cancels_queued_movement_and_runs_first():
    queue, ran = CommandQueue(), []
    queue.put(PRIO_SETTING, None, lambda: ran.append("setting"))
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("move"))
    queue.put(PRIO_STOP, "move-a", lambda: ran.append("stop"))
    queue.drain()
    assert ran == ["stop", "setting"]  # the queued movement was replaced by its key


def test_new_movement_replaces_the_queued_one():
    queue, ran = CommandQueue(), []
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("up"))
    queue.put(PRIO_MOVE, "move-a", lambda: ran.append("down"))
    queue.put(PRIO_MOVE, "move-b", lambda: ran.append("other"))
    queue.drain()
    assert ran == ["down", "other"]


def test_telemetry_publishes_markers_and_mirrors_reverse():
    dev = RecordingDevice()
    act = FakeActuator(position=protocol.POSITION_BOTH_LIMITS_UNSET)
    controls = ActuatorControls(dev, act, CommandQueue())
    controls.publish_telemetry()
    assert dev.values["position_current"] == "limits not set"
    act.position = 89
    controls._on_reverse(None, None, SimpleNamespace(payload=b"1", topic="t", retain=False))
    controls.publish_telemetry()
    assert dev.values["reverse"] == 1
    assert dev.values["position_current"] == "11"  # mirrored display only, the wire value is untouched
