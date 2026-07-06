"""Unit tests for the WbDevice MQTT-conventions helper (fake paho client)."""

import json

from wb_dauerhaft_pro.mqtt import DRIVER_NAME, WbDevice


class FakeClient:
    """Records publish/subscribe calls in place of a real paho client."""

    def __init__(self):
        self.published = {}  # topic -> last value
        self.publish_log = []  # [(topic, value, retain), ...]
        self.subscribed = []

    def publish(self, topic, value, retain=False):
        self.publish_log.append((topic, value, retain))
        self.published[topic] = value

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def message_callback_add(self, topic, callback):
        pass


def test_device_meta_is_single_json():
    c = FakeClient()
    WbDevice(c, "dev1", "Штора")
    meta = json.loads(c.published["/devices/dev1/meta"])
    assert meta["driver"] == DRIVER_NAME
    assert meta["title"] == {"en": "Штора", "ru": "Штора"}
    # legacy English-title subtopic kept for backward compatibility
    assert c.published["/devices/dev1/meta/name"] == "Штора"


def test_pushbutton_publishes_no_retained_value():
    c = FakeClient()
    dev = WbDevice(c, "dev1", "X")
    dev.add_control("up", "pushbutton", 1, initial=None)
    assert "/devices/dev1/controls/up/meta" in c.published
    assert "/devices/dev1/controls/up" not in c.published


def test_set_error_writes_error_topic():
    c = FakeClient()
    dev = WbDevice(c, "dev1", "X")
    dev.set_error("r")
    assert c.published["/devices/dev1/meta/error"] == "r"


def test_unchanged_retained_value_is_not_republished():
    c = FakeClient()
    dev = WbDevice(c, "dev1", "X")
    topic = "/devices/dev1/meta/error"
    dev.set_error("r")
    dev.set_error("r")  # unchanged -> skipped
    assert [e[0] for e in c.publish_log].count(topic) == 1
    dev.set_error("")  # changed -> published
    assert [e[0] for e in c.publish_log].count(topic) == 2


def test_all_publishes_are_retained():
    c = FakeClient()
    dev = WbDevice(c, "dev1", "X")
    dev.add_control("address", "text", 1, readonly=True, initial="0x5F")
    dev.set_error("r")
    assert c.publish_log, "expected some publishes"
    assert all(retain is True for _t, _v, retain in c.publish_log)


def test_command_topic_is_subscribed_and_resubscribable():
    c = FakeClient()
    dev = WbDevice(c, "dev1", "X")
    dev.on_command("up", lambda *a: None)
    assert "/devices/dev1/controls/up/on" in c.subscribed
    c.subscribed.clear()
    dev.resubscribe()
    assert "/devices/dev1/controls/up/on" in c.subscribed
