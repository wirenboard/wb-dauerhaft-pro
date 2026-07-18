"""
Command layer: the TX queue's priority/replace semantics and the angle scales.
"""

import pytest

from wb.dauerhaft_pro import protocol
from wb.dauerhaft_pro.commands import PRIO_MOVE, PRIO_SETTING, PRIO_STOP, CommandQueue


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


def test_angle_scales_round_trip():
    assert protocol.angle_to_raw(90, compressed=False) == 90
    assert protocol.angle_to_raw(0, compressed=True) == 36
    assert protocol.angle_to_raw(180, compressed=True) == 144
    for degrees in (0, 45, 90, 135, 180):
        assert protocol.raw_to_angle(protocol.angle_to_raw(degrees, True), True) == degrees


@pytest.mark.parametrize(
    "frame,expected",
    [
        (protocol.control_angle(0x0B, 0x2C), "0402042c"),  # slat angle, raw 44
        (protocol.control_third_point(0x0B), "04020300"),  # go to the waypoint
        (protocol.set_third_point(0x0B), "020105"),  # store the waypoint
        (protocol.query_position(0x0B), "010102"),  # read position
        (protocol.query_angle(0x0B), "010104"),  # read slat angle
    ],
)
def test_command_frames_match_the_controls_table(frame, expected):
    # The table lists frames as function+length+data, without address and CRC.
    assert frame[1:-2].hex() == expected


def test_learning_frame_goes_to_the_learning_address():
    frame = protocol.set_address(protocol.LEARNING_ADDRESS, 0x5E)
    assert frame[0] == 0xFF and frame[1:-2].hex() == "10015e"
