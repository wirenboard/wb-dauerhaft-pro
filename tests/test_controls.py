"""
Device controls: the control table (address indicator + command controls with
their display order and titles) is published from one place, the command
callbacks enqueue with the right priority/key, and publish_state reflects the
address and availability.
"""

import json
from types import SimpleNamespace

from wb.dauerhaft_pro.commands import CommandQueue
from wb.dauerhaft_pro.controls import DeviceControls, publish_state
from wb.dauerhaft_pro.mqtt import WbDevice


class _RecordingClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value))

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def message_callback_add(self, topic, callback):
        pass


def _make(client=None, **actuator_attrs):
    client = client or _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    cfg = SimpleNamespace(address=0x5F, device_id="dauerhaft_5f")
    actuator = SimpleNamespace(cfg=cfg, online=True, **actuator_attrs)
    return client, dev, actuator


def test_control_table_published_from_one_place():
    """
    create() publishes the whole table: the read-only address indicator at order
    5 and the command controls at their orders, each with a bilingual title.
    """
    client, dev, actuator = _make()
    DeviceControls(dev, actuator, CommandQueue()).create()
    published = dict(client.published)

    addr_meta = json.loads(published["/devices/dauerhaft_5f/controls/address/meta"])
    assert addr_meta["readonly"] is True and addr_meta["order"] == 5
    assert published["/devices/dauerhaft_5f/controls/address"] == "0x5F"

    up_meta = json.loads(published["/devices/dauerhaft_5f/controls/up/meta"])
    assert up_meta["order"] == 1 and up_meta["title"] == {"ru": "Открыть", "en": "Open"}
    apply_meta = json.loads(published["/devices/dauerhaft_5f/controls/address_set/meta"])
    assert apply_meta["order"] == 7 and apply_meta["title"]["en"] == "Set New Address"


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
