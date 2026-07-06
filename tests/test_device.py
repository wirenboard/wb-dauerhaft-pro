"""Unit tests for the Actuator model using a fake transport (no bus, no MQTT)."""

import pytest

from wb_mqtt_dauerhaft_pro import protocol as p
from wb_mqtt_dauerhaft_pro.device import ADDRESS_TIMEOUT_MS, Actuator, ActuatorConfig
from wb_mqtt_dauerhaft_pro.transport import DeviceTimeout, PortConfig


class FakeTransport:
    """Records requests and returns canned, CRC-valid replies."""

    def __init__(self, reply_for=None, raise_timeout=False):
        self.calls = []  # list of (request, response_size, response_timeout_ms, total_timeout_ms)
        self._reply_for = reply_for or self._default_reply
        self._raise_timeout = raise_timeout

    def transceive(self, port, request, response_size, *, response_timeout_ms=None, total_timeout_ms=None):
        self.calls.append((request, response_size, response_timeout_ms, total_timeout_ms))
        if self._raise_timeout:
            raise DeviceTimeout("fake timeout")
        return self._reply_for(request)

    @staticmethod
    def _default_reply(request):
        frame = p.parse_frame(request)
        if frame.function == p.Function.SET_ADDRESS:
            new_addr = frame.data[0]
            return p.build_frame(new_addr, p.Function.SET_ADDRESS, bytes([new_addr, p.SETTING_OK]))
        if frame.function == p.Function.QUERY:
            return p.build_frame(frame.address, p.Function.QUERY, bytes([p.QuerySub.ADDRESS, frame.address]))
        # control: echo the request back (a valid frame)
        return request


def make_actuator(**kwargs):
    cfg = ActuatorConfig(
        mqtt_id="blind1", title="Blind", address=0x5F, port=PortConfig(path="/dev/ttyRS485-2")
    )
    transport = FakeTransport(**kwargs)
    return Actuator(cfg, transport), transport


def test_up_sends_move_up():
    act, t = make_actuator()
    act.up()
    assert t.calls[0][0] == p.control_up(0x5F)
    assert act.online is True


def test_down_sends_move_down():
    act, t = make_actuator()
    act.down()
    assert t.calls[0][0] == p.control_down(0x5F)


def test_stop_sends_stop():
    act, t = make_actuator()
    act.stop()
    assert t.calls[0][0] == p.control_stop(0x5F)


def test_timeout_marks_offline():
    act, t = make_actuator(raise_timeout=True)
    act.up()
    assert act.online is False


def test_ping_updates_online():
    act, _ = make_actuator()
    assert act.ping() is True
    assert act.online is True


def test_set_address_success_follows_new_address():
    act, t = make_actuator()
    assert act.set_address(0x5E) is True
    assert act.cfg.address == 0x5E  # runtime follows the new address
    # the request was sent to the OLD address
    assert p.parse_frame(t.calls[0][0]).address == 0x5F


def test_set_address_uses_long_timeout():
    act, t = make_actuator()
    act.set_address(0x5E)
    # both the per-reply (response) and overall (total) budgets are raised
    assert t.calls[0][2] == ADDRESS_TIMEOUT_MS  # response_timeout_ms
    assert t.calls[0][3] == ADDRESS_TIMEOUT_MS  # total_timeout_ms


def test_set_address_refuses_broadcast():
    act, _ = make_actuator()
    with pytest.raises(ValueError):
        act.set_address(0)


def test_set_address_rejects_out_of_range():
    act, _ = make_actuator()
    with pytest.raises(ValueError):
        act.set_address(256)


def test_set_address_failure_keeps_old_address():
    def reply(request):
        frame = p.parse_frame(request)
        new_addr = frame.data[0]
        # status byte 0x00 => not OK
        return p.build_frame(new_addr, p.Function.SET_ADDRESS, bytes([new_addr, 0x00]))

    act, _ = make_actuator(reply_for=reply)
    assert act.set_address(0x5E) is False
    assert act.cfg.address == 0x5F


def test_error_response_is_logged_but_stays_online(caplog):
    def reply(_request):
        return p.build_frame(0x5F, p.Function.ERROR, bytes([p.ERROR_MARKER, 0x03]))

    act, _ = make_actuator(reply_for=reply)
    with caplog.at_level("WARNING"):
        act.up()
    # the device answered (so it is online) but rejected the command -> logged
    assert act.online is True
    assert any("device error response" in r.getMessage() for r in caplog.records)


def test_stray_frame_from_other_device_does_not_flip_online_offline():
    act, t = make_actuator()
    act.ping()  # a valid reply first -> online
    assert act.online is True
    # now a frame from a DIFFERENT device on the shared bus arrives as the reply
    t._reply_for = lambda _req: p.build_frame(0x99, p.Function.CONTROL, bytes([p.ControlSub.MOVE, 0x64]))
    act.up()
    assert act.online is True  # a stray frame is not our answer and must NOT offline us


def test_active_report_does_not_flip_online_offline():
    act, t = make_actuator()
    act.ping()
    assert act.online is True
    # an unsolicited movement report (0x08) arrives instead of our command's reply
    t._reply_for = lambda _req: p.build_frame(0x5F, p.Function.ACTIVE_REPORT, bytes([0x23, 0x00]))
    act.up()
    assert act.online is True  # wrong function -> not our answer, must NOT offline us
