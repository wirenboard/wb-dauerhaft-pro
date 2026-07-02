"""Unit tests for the Blind device model, using a fake transport (no broker)."""

import pytest

from wb_mqtt_dauerhaft_pro import protocol
from wb_mqtt_dauerhaft_pro import device as d
from wb_mqtt_dauerhaft_pro.transport import DeviceTimeout, PortConfig

PORT = PortConfig(path="/dev/ttyRS485-2")
ADDR = 0x5F


class FakeTransport:
    """Records sent requests; returns canned replies keyed by request hex."""

    def __init__(self):
        self.sent = []
        self.kwargs = []
        self.replies = {}
        self.timeout = False

    def transceive(self, port, request, response_size, **kw):
        self.sent.append(request)
        self.kwargs.append(kw)
        if self.timeout:
            raise DeviceTimeout("fake")
        reply = self.replies.get(request.hex())
        if reply is None:
            # unmatched request == device didn't answer (models a real bus)
            raise DeviceTimeout(f"no canned reply for {request.hex()}")
        return reply

    def canned(self, request: bytes, reply: bytes):
        self.replies[request.hex()] = reply


def make(blind_type=d.BlindType.ROLLER, reverse=False):
    ft = FakeTransport()
    cfg = d.BlindConfig(name="test", address=ADDR, port=PORT,
                        blind_type=blind_type, reverse_position=reverse)
    return d.Blind(cfg, ft), ft


# ------------------------------------------------------------------ #
# commands emit the right frames
# ------------------------------------------------------------------ #

def test_open_close_stop_frames():
    b, ft = make()
    b.open()
    assert ft.sent[-1] == protocol.control_open(ADDR)
    b.close()
    assert ft.sent[-1] == protocol.control_close(ADDR)
    b.stop()
    assert ft.sent[-1] == protocol.control_stop(ADDR)


def test_set_position_frame():
    b, ft = make()
    b.set_position(30)
    assert ft.sent[-1] == protocol.control_move(ADDR, 30)


def test_set_position_reverse_inverts():
    b, ft = make(reverse=True)
    b.set_position(30)
    assert ft.sent[-1] == protocol.control_move(ADDR, 70)


def test_set_position_clamped():
    b, ft = make()
    b.set_position(150)
    assert ft.sent[-1] == protocol.control_move(ADDR, 100)


# ------------------------------------------------------------------ #
# state updates from replies
# ------------------------------------------------------------------ #

def test_command_reply_updates_position():
    b, ft = make()
    # device echoes current position 0x2A (42) after the move
    ft.canned(protocol.control_move(ADDR, 30),
              protocol.build_frame(ADDR, protocol.Function.CONTROL,
                                   [protocol.ControlSub.MOVE, 0x2A]))
    b.set_position(30)
    assert b.state.position == 42
    assert b.state.online is True


def test_poll_updates_position_marker_and_motion():
    b, ft = make()
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY,
                                   [protocol.QuerySub.POSITION, 0xFC]))  # both limits unset
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY,
                                   [protocol.QuerySub.STATE, protocol.MotorState.MOVING_UP]))
    b.poll()
    assert b.state.online is True
    assert b.state.position is None
    assert b.state.position_marker == "both_limits_unset"
    assert b.state.motion == protocol.MotorState.MOVING_UP


def test_poll_reverse_position():
    b, ft = make(reverse=True)
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY,
                                   [protocol.QuerySub.POSITION, 30]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY,
                                   [protocol.QuerySub.STATE, 0]))
    b.poll()
    assert b.state.position == 70  # 100 - 30


def test_timeout_marks_offline():
    b, ft = make()
    ft.timeout = True
    b.poll()
    assert b.state.online is False


# ------------------------------------------------------------------ #
# capability auto-detection
# ------------------------------------------------------------------ #

def test_angle_disabled_on_error_reply():
    b, ft = make(blind_type=d.BlindType.LAMELLA)
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.POSITION, 50]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.STATE, 0]))
    ft.canned(protocol.query_angle(ADDR),
              protocol.build_frame(ADDR, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03]))
    # firmware also unsupported here
    ft.canned(protocol.query_firmware(ADDR),
              protocol.build_frame(ADDR, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03]))
    b.poll()
    assert b._angle_supported is False  # pylint: disable=protected-access
    # second poll must NOT send an angle query anymore
    ft.sent.clear()
    b.poll()
    assert protocol.query_angle(ADDR) not in ft.sent


def test_roller_never_queries_angle():
    b, ft = make(blind_type=d.BlindType.ROLLER)
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.POSITION, 50]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.STATE, 0]))
    ft.canned(protocol.query_firmware(ADDR),
              protocol.build_frame(ADDR, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03]))
    b.poll()
    assert protocol.query_angle(ADDR) not in ft.sent


def test_firmware_decoded_once():
    b, ft = make()
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.POSITION, 50]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.STATE, 0]))
    ft.canned(protocol.query_firmware(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY,
                                   [protocol.QuerySub.FIRMWARE, 0x01, 0x03, 0x02]))
    b.poll()
    assert b.state.firmware == "2.3.1"
    # once known, firmware is not queried again
    ft.sent.clear()
    b.poll()
    assert protocol.query_firmware(ADDR) not in ft.sent


# ------------------------------------------------------------------ #
# Stage 4: service commands, address, online hysteresis
# ------------------------------------------------------------------ #

def test_service_command_frames():
    b, ft = make()
    b.set_upper_limit()
    assert ft.sent[-1] == protocol.setting(ADDR, protocol.SettingSub.SET_UPPER_LIMIT)
    b.set_lower_limit()
    assert ft.sent[-1] == protocol.setting(ADDR, protocol.SettingSub.SET_LOWER_LIMIT)
    b.delete_limits()
    assert ft.sent[-1] == protocol.setting(ADDR, protocol.SettingSub.DELETE_LIMITS)
    b.set_third_point()
    assert ft.sent[-1] == protocol.setting(ADDR, protocol.SettingSub.SET_THIRD_POINT)
    b.change_direction()
    assert ft.sent[-1] == protocol.setting(ADDR, protocol.SettingSub.CHANGE_DIRECTION)
    b.third_point()  # control (go to), distinct from set_third_point (store)
    assert ft.sent[-1] == protocol.control_third_point(ADDR)


def test_query_address_returns_int():
    b, ft = make()
    ft.canned(protocol.query_address(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.ADDRESS, ADDR]))
    assert b.query_address() == ADDR


def test_set_address_success_follows_at_runtime():
    b, ft = make()
    # device replies FROM the new address (per spec)
    ft.canned(protocol.set_address(ADDR, 0x5E),
              protocol.build_frame(0x5E, protocol.Function.SET_ADDRESS, [0x5E, protocol.SETTING_OK]))
    assert b.set_address(0x5E) is True
    assert b.cfg.address == 0x5E  # runtime config follows so polling continues


def test_set_address_refuses_broadcast():
    b, _ = make()
    with pytest.raises(ValueError):
        b.set_address(0x00)


def test_online_hysteresis():
    b, ft = make()  # default offline_after_misses = 3
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.POSITION, 50]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.STATE, 0]))
    ft.canned(protocol.query_firmware(ADDR),
              protocol.build_frame(ADDR, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03]))
    b.poll()
    assert b.state.online is True

    ft.timeout = True
    b.poll(); assert b.state.online is True   # miss 1
    b.poll(); assert b.state.online is True   # miss 2
    b.poll(); assert b.state.online is False  # miss 3 -> offline

    ft.timeout = False
    b.poll(); assert b.state.online is True   # recovered


def test_config_commands_use_long_timeout():
    # Setting/address ops write flash and can be slow; must use the 5 s budget,
    # not the default 500 ms (a live 0x0B set-limit timed out at 500 ms).
    b, ft = make()
    ok = protocol.build_frame(ADDR, protocol.Function.SETTING,
                              [protocol.SettingSub.SET_UPPER_LIMIT, protocol.SETTING_OK])
    ft.canned(protocol.setting(ADDR, protocol.SettingSub.SET_UPPER_LIMIT), ok)
    b.set_upper_limit()
    assert ft.kwargs[-1].get("total_timeout_ms") == d.CONFIG_TIMEOUT_MS

    # a plain poll query must NOT force the long timeout (uses transport default)
    ft.canned(protocol.query_position(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.POSITION, 50]))
    ft.canned(protocol.query_state(ADDR),
              protocol.build_frame(ADDR, protocol.Function.QUERY, [protocol.QuerySub.STATE, 0]))
    ft.canned(protocol.query_firmware(ADDR),
              protocol.build_frame(ADDR, protocol.Function.ERROR, [protocol.ERROR_MARKER, 0x03]))
    ft.kwargs.clear()
    b.poll()
    assert all(kw.get("total_timeout_ms") is None for kw in ft.kwargs)
