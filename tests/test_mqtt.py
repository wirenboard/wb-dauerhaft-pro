"""
MQTT device-helper unit tests: unchanged retained values must not be
republished (the poll loop calls set_value/set_error on every cycle), and the
shared command dispatcher must drop retained replays while logging accepted
commands.
"""

import logging
from types import SimpleNamespace

from wb.dauerhaft_pro.mqtt import WbDevice


class FakeMessageInfo:
    """Just enough of paho's MQTTMessageInfo for the confirmation path."""

    def __init__(self):
        self.waited = False

    def wait_for_publish(self, _timeout=None):
        self.waited = True

    def is_published(self):
        return True


class RecordingClient:
    info_factory = FakeMessageInfo

    def __init__(self):
        self.published = []
        self.infos = []
        self.callbacks = {}

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value, retain))
        info = self.info_factory()
        self.infos.append(info)
        return info

    def subscribe(self, topic):
        pass

    def message_callback_add(self, topic, callback):
        self.callbacks[topic] = callback


def test_unchanged_retained_values_are_not_republished():
    """
    An unchanged retained value is not republished; a changed value goes out
    once, retained.
    """
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    before = len(client.published)

    dev.set_value("address", "0x5F")  # unchanged: must be deduplicated
    dev.set_error("")
    dev.set_error("")  # unchanged error state: ditto
    # only the first set_error went out: the device error and its mirror on
    # the one control
    assert len(client.published) == before + 2

    dev.set_value("address", "0x5E")  # a real change is published, retained
    assert client.published[-1] == ("/devices/dauerhaft_test/controls/address", "0x5E", True)


def test_command_dispatcher_drops_retained_and_logs_accepted(caplog):
    """
    The shared <control>/on dispatcher drops a retained replay (a stale command
    must not move the actuator on a daemon restart) and delivers a fresh
    command, logging it at INFO so user actions leave a journal trace.
    """
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    delivered = []
    dev.on_command("up", lambda _c, _u, msg: delivered.append(msg))
    handler = client.callbacks["/devices/dauerhaft_test/controls/up/on"]

    handler(None, None, SimpleNamespace(retain=True, topic="t", payload=b"1"))
    assert not delivered  # retained: dropped

    with caplog.at_level(logging.INFO, logger="wb.dauerhaft_pro.mqtt"):
        handler(None, None, SimpleNamespace(retain=False, topic="t", payload=b"1"))
    assert [msg.payload for msg in delivered] == [b"1"]  # fresh: delivered
    assert "dauerhaft_test: command up <- 1" in caplog.text


def test_error_is_mirrored_onto_controls():
    """
    set_error() mirrors the device-level error onto every control and clears
    it the same way — the panel's device list reflects only control errors.
    """
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    dev.add_control("up", "pushbutton", 1, initial=None)
    dev.set_error("r")
    assert ("/devices/dauerhaft_test/meta/error", "r", True) in client.published
    assert ("/devices/dauerhaft_test/controls/address/meta/error", "r", True) in client.published
    assert ("/devices/dauerhaft_test/controls/up/meta/error", "r", True) in client.published
    client.published.clear()
    dev.set_error("")
    assert ("/devices/dauerhaft_test/meta/error", "", True) in client.published
    assert ("/devices/dauerhaft_test/controls/address/meta/error", "", True) in client.published


def test_remove_clears_the_mirrored_control_errors():
    """
    remove() clears the mirrored control errors along with everything else, so
    no retained topic survives a shutdown.
    """
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    dev.set_error("r")
    dev.remove()
    assert ("/devices/dauerhaft_test/controls/address/meta/error", None, True) in client.published


def test_remove_waits_for_the_retained_clears():
    """
    remove() must confirm delivery of every publish still in flight: the
    daemon calls client.stop() right after it, which kills the network
    thread, and an unconfirmed clear dies in the paho queue — a ghost device
    in the panel.
    """

    class InFlightInfo(FakeMessageInfo):
        """Confirms only when awaited — a publish the network has not sent yet."""

        def is_published(self):
            return self.waited

    client = RecordingClient()
    client.info_factory = InFlightInfo
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    dev.remove()
    assert client.infos and all(info.waited for info in client.infos)


def test_confirmed_receipts_do_not_pile_up():
    """
    Confirmed publish receipts are pruned as new publishes happen: the device
    lives for months with wait_published() only called on shutdown, so
    hoarding every state change's receipt until then would be an unbounded
    leak.
    """
    # pylint: disable=protected-access
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    for value in range(50):
        dev.set_value("address", str(value))
    assert len(dev._pending) <= 2  # the fake confirms instantly: no hoard


def test_wait_published_survives_unconfirmed_publishes(caplog):
    """
    A publish that cannot confirm (paho raises RuntimeError from
    wait_for_publish on a dead connection) must not raise out of
    wait_published — its callers are best-effort — but must leave a warning.
    """

    class RefusingInfo(FakeMessageInfo):
        def wait_for_publish(self, _timeout=None):
            raise RuntimeError("not connected")

        def is_published(self):
            return False

    client = RecordingClient()
    client.info_factory = RefusingInfo
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    with caplog.at_level(logging.WARNING, logger="wb.dauerhaft_pro.mqtt"):
        dev.wait_published(timeout=0.1)
    assert "unconfirmed" in caplog.text


def test_republish_restores_every_topic():
    """
    republish() re-sends every retained topic of the device — recovery after a
    broker restart, dedup notwithstanding.
    """
    # after a broker restart the retained state may be gone: republish() must
    # replay the full device, dedup notwithstanding
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    dev.set_error("")
    published_once = sorted(client.published)
    client.published.clear()
    dev.republish()
    assert sorted(client.published) == published_once
