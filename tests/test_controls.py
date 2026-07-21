"""
Device controls: the control table is published from one place with the right
orders/titles, the slat-angle controls are gated on the config, the callbacks
enqueue with the right priority/key/method, and the telemetry publishes
position markers and mirrors reverse.
"""

import json
from types import SimpleNamespace

from wb.dauerhaft_pro import protocol
from wb.dauerhaft_pro.commands import CommandQueue
from wb.dauerhaft_pro.controls import DeviceControls, publish_state
from wb.dauerhaft_pro.mqtt import WbDevice


class _RecordingClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value))

    def subscribe(self, topic):
        pass

    def message_callback_add(self, topic, callback):
        pass


def _cfg(**over):
    base = dict(
        address=0x5F,
        device_id="dauerhaft_5f",
        learning_type="physical_button",
        slat_angle_mode="none",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _make(client=None, cfg=None, **actuator_attrs):
    client = client or _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    actuator = SimpleNamespace(cfg=cfg or _cfg(), online=True, **actuator_attrs)
    return client, dev, actuator


def test_control_table_published_from_one_place():
    """
    create() publishes the whole table from one place: the read-only position
    and address indicators plus the command controls, each at its order.
    """
    client, dev, actuator = _make()
    DeviceControls(dev, actuator, CommandQueue()).create()
    published = dict(client.published)

    pos_meta = json.loads(published["/devices/dauerhaft_5f/controls/position_current/meta"])
    assert pos_meta["readonly"] is True and pos_meta["order"] == 4
    addr_meta = json.loads(published["/devices/dauerhaft_5f/controls/address/meta"])
    assert addr_meta["readonly"] is True and addr_meta["order"] == 5
    up_meta = json.loads(published["/devices/dauerhaft_5f/controls/up/meta"])
    assert up_meta["order"] == 1 and up_meta["title"] == {"ru": "Открыть", "en": "Open"}
    rev_meta = json.loads(published["/devices/dauerhaft_5f/controls/reverse/meta"])
    assert rev_meta["type"] == "switch" and rev_meta["order"] == 8


def test_slat_angle_controls_only_for_lamella_curtains():
    """
    The slat-angle controls exist only when slat_angle_mode is not "none".
    """
    client_off, dev_off, act_off = _make()  # slat_angle_mode="none"
    DeviceControls(dev_off, act_off, CommandQueue()).create()
    assert "/devices/dauerhaft_5f/controls/slat_angle/meta" not in dict(client_off.published)

    client_on, dev_on, act_on = _make(cfg=_cfg(slat_angle_mode="direct"))
    DeviceControls(dev_on, act_on, CommandQueue()).create()
    assert "/devices/dauerhaft_5f/controls/slat_angle/meta" in dict(client_on.published)


def test_open_enqueues_move_and_stop_takes_priority():
    """
    Open enqueues a movement; a following stop shares the move key, so it both
    outranks and cancels the queued open — only the stop runs.
    """
    ran = []
    _client, dev, actuator = _make(up=lambda: ran.append("up"), stop=lambda: ran.append("stop"))
    queue = CommandQueue()
    controls = DeviceControls(dev, actuator, queue)
    msg = SimpleNamespace(retain=False, topic="t")
    # invoking the paho-signature callbacks directly is the point of the test
    # pylint: disable=protected-access
    controls._on_up(None, None, msg)
    controls._on_stop(None, None, msg)
    queue.drain()
    assert ran == ["stop"]


def test_address_button_uses_the_configured_learning_method():
    """
    The single address button writes via the learning window for
    physical_button and by unicast for widget_command.
    """
    calls = []

    def apply(learning_type):
        _client, dev, actuator = _make(
            cfg=_cfg(learning_type=learning_type),
            set_address=lambda addr: calls.append(("unicast", addr)),
            set_address_learning=lambda addr: calls.append(("learning", addr)),
        )
        queue = CommandQueue()
        controls = DeviceControls(dev, actuator, queue)
        # pylint: disable=protected-access
        controls._addr_target = 0x42
        controls._on_addr_set(None, None, SimpleNamespace(retain=False, topic="t"))
        queue.drain()

    apply("physical_button")
    apply("widget_command")
    assert calls == [("learning", 0x42), ("unicast", 0x42)]


def test_telemetry_publishes_markers_and_mirrors_reverse():
    """
    A limits-unset marker is published as text; reverse mirrors the displayed
    position only (the wire value is untouched).
    """
    client = _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    state = {"pos": protocol.POSITION_BOTH_LIMITS_UNSET}
    actuator = SimpleNamespace(
        cfg=_cfg(),
        online=True,
        query_position=lambda: state["pos"],
        query_angle_raw=lambda: None,
    )
    controls = DeviceControls(dev, actuator, CommandQueue())

    controls.publish_telemetry()
    assert dict(client.published)["/devices/dauerhaft_5f/controls/position_current"] == "limits not set"

    state["pos"] = 89
    # pylint: disable=protected-access
    controls._on_reverse(None, None, SimpleNamespace(payload=b"1", topic="t", retain=False))
    controls.publish_telemetry()
    published = dict(client.published)
    assert published["/devices/dauerhaft_5f/controls/reverse"] == "1"
    assert published["/devices/dauerhaft_5f/controls/position_current"] == "11"  # mirrored display only


def test_publish_state_reflects_availability_and_address():
    """
    publish_state clears /meta/error when online, sets "r" when offline, and
    publishes the current address.
    """
    client, dev, actuator = _make()
    publish_state(dev, actuator)
    published = dict(client.published)
    assert published["/devices/dauerhaft_5f/meta/error"] == ""  # online
    assert published["/devices/dauerhaft_5f/controls/address"] == "0x5F"

    actuator.online = False
    publish_state(dev, actuator)
    assert dict(client.published)["/devices/dauerhaft_5f/meta/error"] == "r"  # offline
