"""Unit tests for the daemon's pure logic (dispatch, control wiring, state, resubscribe).

conftest stubs ``mqttrpc`` so main imports without the real package.
"""

import pytest

from wb_dauerhaft_pro import main


class FakeAct:
    def __init__(self, address=0x5F, online=True):
        self.cfg = type("Cfg", (), {"address": address})()
        self.online = online
        self.calls = []

    def up(self):
        self.calls.append("up")

    def down(self):
        self.calls.append("down")

    def stop(self):
        self.calls.append("stop")

    def set_address(self, new_address):
        self.calls.append(("set_address", new_address))


class FakeDev:
    def __init__(self, device_id="dev1"):
        self.id = device_id
        self.controls = []
        self.callbacks = {}
        self.errors = []
        self.values = {}
        self.resubscribed = 0

    def add_control(self, name, control_type, order, **_kw):
        self.controls.append((name, control_type))

    def on_command(self, name, callback):
        self.callbacks[name] = callback

    def set_error(self, error):
        self.errors.append(error)

    def set_value(self, name, value):
        self.values[name] = value

    def resubscribe(self):
        self.resubscribed += 1


class FakeMsg:
    def __init__(self, payload):
        self.payload = payload


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def test_dispatch_routes_motion():
    act = FakeAct()
    main.dispatch(act, "up", "")
    main.dispatch(act, "down", "")
    main.dispatch(act, "stop", "")
    assert act.calls == ["up", "down", "stop"]


def test_dispatch_set_address_parses_int():
    act = FakeAct()
    main.dispatch(act, "set_address", "94")
    assert act.calls == [("set_address", 94)]


def test_dispatch_set_address_bad_payload_raises():
    act = FakeAct()
    with pytest.raises(ValueError):
        main.dispatch(act, "set_address", "not-a-number")


def test_dispatch_unknown_action_is_ignored():
    act = FakeAct()
    main.dispatch(act, "nonsense", "")
    assert act.calls == []


# --------------------------------------------------------------------------- #
# build_controls: control set + the command-queue tuple shape
# --------------------------------------------------------------------------- #


def test_build_controls_creates_expected_controls():
    dev, act = FakeDev(), FakeAct()
    main.build_controls(dev, act, lambda *a: None)
    names = {name for name, _type in dev.controls}
    assert {"up", "down", "stop", "address", "set_address"} <= names


def test_command_callback_enqueues_dev_act_action_value():
    dev, act = FakeDev(), FakeAct()
    recorded = []
    main.build_controls(dev, act, lambda *args: recorded.append(args))

    dev.callbacks["up"](None, None, FakeMsg(b""))
    assert recorded[-1] == (dev, act, "up", "")

    dev.callbacks["set_address"](None, None, FakeMsg(b"94"))
    assert recorded[-1] == (dev, act, "set_address", "94")


# --------------------------------------------------------------------------- #
# publish_state
# --------------------------------------------------------------------------- #


def test_publish_state_online_clears_error_and_publishes_address():
    dev, act = FakeDev(), FakeAct(address=0x5F, online=True)
    main.publish_state(dev, act)
    assert dev.errors[-1] == ""  # available
    assert dev.values["address"] == "0x5F"
    assert dev.values["set_address"] == 0x5F


def test_publish_state_offline_sets_error():
    dev, act = FakeDev(), FakeAct(online=False)
    main.publish_state(dev, act)
    assert dev.errors[-1] == "r"


# --------------------------------------------------------------------------- #
# resubscribe_all (the on_connect body — fix #1)
# --------------------------------------------------------------------------- #


def test_resubscribe_all_resubscribes_and_clears_rpc_cache():
    d1, d2 = FakeDev("a"), FakeDev("b")
    rpc = type("Rpc", (), {"subscribes": {("wb-mqtt-serial", "port", "Load")}})()
    main.resubscribe_all([(d1, FakeAct()), (d2, FakeAct())], rpc)
    assert d1.resubscribed == 1
    assert d2.resubscribed == 1
    assert rpc.subscribes == set()
