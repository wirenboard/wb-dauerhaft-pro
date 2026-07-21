"""
MQTT device-helper unit tests: unchanged retained values must not be
republished — the poll loop calls set_value/set_error on every cycle.
"""

from wb.dauerhaft_pro.mqtt import WbDevice


class RecordingClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value, retain))

    def subscribe(self, topic):
        pass

    def message_callback_add(self, topic, callback):
        pass


def test_unchanged_retained_values_are_not_republished():
    """
    Неизменённые retained-значения не публикуются повторно; изменённое значение
    уходит один раз с retain.
    """
    client = RecordingClient()
    dev = WbDevice(client, "dauerhaft_test", "Тест")
    dev.add_control("address", "text", 5, readonly=True, initial="0x5F")
    before = len(client.published)

    dev.set_value("address", "0x5F")  # unchanged: must be deduplicated
    dev.set_error("")
    dev.set_error("")  # unchanged error state: ditto
    assert len(client.published) == before + 1  # only the first set_error went out

    dev.set_value("address", "0x5E")  # a real change is published, retained
    assert client.published[-1] == ("/devices/dauerhaft_test/controls/address", "0x5E", True)


def test_republish_restores_every_topic():
    """
    republish() переиздаёт все retained-топики устройства — восстановление после
    рестарта брокера, несмотря на дедуп.
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
