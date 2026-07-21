"""
Device controls: the whole control table is published from one place with the
right orders/titles/units, the slat-angle controls are gated on the config,
reverse is seeded from the config and validates its payload, the callbacks
enqueue with the right priority/key/method and scale, and the telemetry renders
markers, units and the reverse mirror.
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
        reverse=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _make(client=None, cfg=None, **actuator_attrs):
    client = client or _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    actuator = SimpleNamespace(cfg=cfg or _cfg(), online=True, **actuator_attrs)
    return client, dev, actuator


def _msg(payload=b"", topic="t"):
    return SimpleNamespace(payload=payload, topic=topic, retain=False)


def test_control_table_published_from_one_place():
    """
    create() publishes the whole table: read-only position/address indicators
    and the command controls, each at its order (address decimal, reverse a
    switch at order 8).
    """
    client, dev, actuator = _make()
    DeviceControls(dev, actuator, CommandQueue()).create()
    published = dict(client.published)

    pos_meta = json.loads(published["/devices/dauerhaft_5f/controls/position_current/meta"])
    assert pos_meta["readonly"] is True and pos_meta["order"] == 4
    addr_meta = json.loads(published["/devices/dauerhaft_5f/controls/address/meta"])
    assert addr_meta["readonly"] is True and addr_meta["order"] == 5
    assert published["/devices/dauerhaft_5f/controls/address"] == "95"  # decimal, matches "New Address"
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


def test_reverse_seeded_from_config_and_validates_payload():
    """
    reverse starts from the persisted config value; a b"0"/b"1" payload toggles
    it and any other payload is ignored (unchanged).
    """
    _client, dev, actuator = _make(cfg=_cfg(reverse=True))
    controls = DeviceControls(dev, actuator, CommandQueue())
    # pylint: disable=protected-access
    assert controls._reverse is True  # seeded from config, not hard-off
    controls._on_reverse(None, None, _msg(b"0"))
    assert controls._reverse is False
    controls._on_reverse(None, None, _msg(b"x"))
    assert controls._reverse is False  # malformed payload ignored, unchanged
    controls._on_reverse(None, None, _msg(b"1"))
    assert controls._reverse is True


def test_open_enqueues_move_and_stop_takes_priority():
    """
    Open enqueues a movement; a following stop shares the move key, so it both
    outranks and cancels the queued open — only the stop runs.
    """
    ran = []
    _client, dev, actuator = _make(up=lambda: ran.append("up"), stop=lambda: ran.append("stop"))
    queue = CommandQueue()
    controls = DeviceControls(dev, actuator, queue)
    # invoking the paho-signature callbacks directly is the point of the test
    # pylint: disable=protected-access
    controls._on_up(None, None, _msg())
    controls._on_stop(None, None, _msg())
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
        controls._on_addr_set(None, None, _msg())
        queue.drain()

    apply("physical_button")
    apply("widget_command")
    assert calls == [("learning", 0x42), ("unicast", 0x42)]


def test_slat_angle_command_scales_and_gates_the_value():
    """
    _on_slat_angle converts degrees to the wire byte per the scale (direct as
    is, compressed 0°→36) and ignores an out-of-range value.
    """
    raw = []
    _c, dev, act = _make(cfg=_cfg(slat_angle_mode="direct"), set_angle_raw=raw.append)
    queue = CommandQueue()
    controls = DeviceControls(dev, act, queue)
    # pylint: disable=protected-access
    controls._on_slat_angle(None, None, _msg(b"90"))
    queue.drain()
    controls._on_slat_angle(None, None, _msg(b"200"))  # out of 0..180
    queue.drain()
    assert raw == [90]  # direct scale; 200 was rejected, not queued

    raw_c = []
    _c2, dev2, act2 = _make(cfg=_cfg(slat_angle_mode="compressed"), set_angle_raw=raw_c.append)
    queue2 = CommandQueue()
    controls2 = DeviceControls(dev2, act2, queue2)
    controls2._on_slat_angle(None, None, _msg(b"0"))
    queue2.drain()
    assert raw_c == [36]  # compressed scale maps 0° -> raw 36


def test_telemetry_renders_markers_units_and_reverse():
    """
    Position renders a Russian limits marker or "N %"; reverse mirrors the shown
    position; the slat angle is published to the read-only indicator as "N °".
    """
    client = _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    state = {"pos": protocol.POSITION_BOTH_LIMITS_UNSET, "raw": None}
    actuator = SimpleNamespace(
        cfg=_cfg(slat_angle_mode="direct"),
        online=True,
        query_position=lambda: state["pos"],
        query_angle_raw=lambda: state["raw"],
    )
    controls = DeviceControls(dev, actuator, CommandQueue())

    controls.publish_telemetry()
    assert dict(client.published)["/devices/dauerhaft_5f/controls/position_current"] == "пределы не заданы"

    state["pos"], state["raw"] = 89, 135
    # pylint: disable=protected-access
    controls._on_reverse(None, None, _msg(b"1"))
    controls.publish_telemetry()
    published = dict(client.published)
    assert (
        published["/devices/dauerhaft_5f/controls/position_current"] == "11 %"
    )  # 100-89, mirrored, with unit
    assert published["/devices/dauerhaft_5f/controls/slat_angle_current"] == "135 °"


def test_publish_state_reflects_availability_and_address():
    """
    publish_state clears /meta/error when online, sets "r" when offline, and
    publishes the current address in decimal.
    """
    client, dev, actuator = _make()
    publish_state(dev, actuator)
    published = dict(client.published)
    assert published["/devices/dauerhaft_5f/meta/error"] == ""  # online
    assert published["/devices/dauerhaft_5f/controls/address"] == "95"

    actuator.online = False
    publish_state(dev, actuator)
    assert dict(client.published)["/devices/dauerhaft_5f/meta/error"] == "r"  # offline
