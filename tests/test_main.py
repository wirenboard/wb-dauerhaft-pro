"""
Daemon entry-point unit tests: a broken config ends with the NOTCONFIGURED exit
code and a visible MQTT announcement, and the journald stream detection does
not misfire.
"""

import json
import os
import signal
import threading
from types import SimpleNamespace

from wb.dauerhaft_pro import main as main_mod

ONE_DEVICE = {
    "devices": [
        {
            "device_id": "dauerhaft_test",
            "device_name": "Тест",
            "curtain_type": "curtain",
            "learning_type": "none",
            "rs485_address": 1,
            "port": "/dev/ttyRS485-1",
        }
    ]
}


class FakeMessageInfo:
    """
    Just enough of paho's MQTTMessageInfo for the confirmation path. Confirms
    only when awaited — a publish the network thread has not sent yet — so the
    test proves the announcement actually waits for every receipt.
    """

    def __init__(self):
        self.waited = False

    def wait_for_publish(self, _timeout=None):
        self.waited = True

    def is_published(self):
        return self.waited


class FakeMQTTClient:
    """Just enough of wb_common's MQTTClient for the announcement path."""

    instances = []

    def __init__(self, client_id, broker_url=None):
        self.client_id = client_id
        self._client_id = client_id.encode()  # read by mqttrpc's TMQTTRPCClient
        self.broker_url = broker_url
        self.published = []
        self.infos = []
        self.started = False
        self.stopped = False
        FakeMQTTClient.instances.append(self)

    def start(self, retry_first_connection=False):  # pylint: disable=unused-argument
        self.started = True

    def stop(self):
        self.stopped = True

    def is_connected(self):
        return self.started and not self.stopped

    def wait_for_connection(self, stop_requested):
        """Like wb-common's: connected wins unless a stop was already requested."""
        while not stop_requested.wait(0.05):
            if self.is_connected():
                return True
        return False

    def will_set(self, topic, payload=None, qos=0, retain=False):
        pass

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value, retain))
        info = FakeMessageInfo()
        self.infos.append(info)
        return info

    def subscribe(self, topic):
        pass

    def message_callback_add(self, topic, callback):
        pass


def test_broken_config_returns_notconfigured_and_announces(tmp_path, monkeypatch):
    """
    A broken config ends the daemon with exit code 6 (NOTCONFIGURED), no
    traceback, and the reason is handed to the MQTT announcement.
    """
    # the private seam is the point under test
    # pylint: disable=protected-access
    conf = tmp_path / "bad.conf"
    conf.write_text("{oops", encoding="utf-8")
    announced = []
    monkeypatch.setattr(main_mod, "_announce_config_error", lambda _url, msg: announced.append(msg))
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", str(conf)])
    assert main_mod.main() == main_mod.EXIT_CONFIG_ERROR
    assert announced and "not valid JSON" in announced[0]


def test_config_error_is_published_retained(monkeypatch):
    """
    The announcement publishes the error text retained on the driver-status
    pseudo-device, confirms delivery (paho publishes asynchronously — stopping
    right away would race the network thread) and closes the connection, so
    the panel keeps showing why the daemon is down.
    """
    # pylint: disable=protected-access
    FakeMQTTClient.instances.clear()
    monkeypatch.setattr(main_mod, "MQTTClient", FakeMQTTClient)
    main_mod._announce_config_error("unix:///run/mosquitto.sock", "device ids must be unique")
    client = FakeMQTTClient.instances[-1]
    assert client.started and client.stopped
    assert (
        "/devices/wb-dauerhaft-pro/controls/config_error",
        "device ids must be unique",
        True,
    ) in client.published
    assert client.infos and all(info.waited for info in client.infos)


def test_config_error_announce_survives_a_dead_broker(monkeypatch):
    """
    A dead broker must not turn the config-error exit into a traceback: the
    announcement is best-effort, the journal message stays the fallback.
    """
    # pylint: disable=protected-access

    class DeadBrokerClient(FakeMQTTClient):
        def start(self, retry_first_connection=False):
            raise RuntimeError("broker down")

    monkeypatch.setattr(main_mod, "MQTTClient", DeadBrokerClient)
    main_mod._announce_config_error("unix:///run/mosquitto.sock", "x")  # must not raise


def test_journal_detection_rejects_foreign_streams(monkeypatch):
    """
    The journal-stream detector stays off without $JOURNAL_STREAM and on a
    malformed value — otherwise console logs would silently disappear.
    """
    # the detector is internal by design, but its parsing must be pinned
    # pylint: disable=protected-access
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert main_mod._detect_journal_stderr() is False  # not under systemd
    monkeypatch.setenv("JOURNAL_STREAM", "not:numbers")
    assert main_mod._detect_journal_stderr() is False  # malformed value


def test_journal_detection_accepts_own_stream(monkeypatch):
    """
    When (dev, inode) from $JOURNAL_STREAM match stderr, the detector returns
    True (otherwise systemd would get the StreamHandler and per-level journal
    priorities would be lost) — pins the load-bearing == comparison.
    """
    # pylint: disable=protected-access
    monkeypatch.setattr("os.fstat", lambda _fd: SimpleNamespace(st_dev=42, st_ino=1337))
    monkeypatch.setenv("JOURNAL_STREAM", "42:1337")
    assert main_mod._detect_journal_stderr() is True


def _write_config(tmp_path, content) -> str:
    conf = tmp_path / "wb-dauerhaft-pro.conf"
    conf.write_text(json.dumps(content), encoding="utf-8")
    return str(conf)


def test_no_devices_clears_the_config_error_and_exits_notrunning(tmp_path, monkeypatch):
    """
    An empty device list is not a config error: the stale config-error report
    is cleared (retained None on its topics) and the daemon exits with 7,
    which the unit treats as a success.
    """
    FakeMQTTClient.instances.clear()
    monkeypatch.setattr(main_mod, "MQTTClient", FakeMQTTClient)
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", _write_config(tmp_path, {"devices": []})])
    assert main_mod.main() == main_mod.EXIT_NOTRUNNING
    client = FakeMQTTClient.instances[-1]
    assert ("/devices/wb-dauerhaft-pro/controls/config_error", None, True) in client.published
    assert client.stopped


def test_rejected_login_exits_invalidargument(tmp_path, monkeypatch):
    """
    A broker that rejects the login (CONNACK 5) is a configuration problem:
    the daemon stops before creating any device and exits with 2 instead of
    letting paho retry forever.
    """

    class RejectingBrokerClient(FakeMQTTClient):
        def start(self, retry_first_connection=False):
            super().start(retry_first_connection)
            self.on_connect(self, None, None, 5)  # pylint: disable=no-member  # set by Daemon

    RejectingBrokerClient.instances.clear()
    monkeypatch.setattr(main_mod, "MQTTClient", RejectingBrokerClient)
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", _write_config(tmp_path, ONE_DEVICE)])
    assert main_mod.main() == main_mod.EXIT_INVALIDARGUMENT
    client = RejectingBrokerClient.instances[-1]
    assert client.stopped
    assert not any(topic.startswith("/devices/dauerhaft_test/") for topic, _v, _r in client.published)


def test_signal_while_waiting_for_the_broker_exits_success(tmp_path, monkeypatch, caplog):
    """
    With the broker down the daemon waits instead of exiting with 1; SIGTERM
    during that wait ends it with 0 and a log line saying the retained topics
    could not be removed.
    """

    class DownBrokerClient(FakeMQTTClient):
        def is_connected(self):
            return False

    DownBrokerClient.instances.clear()
    monkeypatch.setattr(main_mod, "MQTTClient", DownBrokerClient)
    # main() replaces the root handlers, which would detach caplog; the seam is private by design
    monkeypatch.setattr(main_mod, "_setup_logging", lambda _debug: None)  # pylint: disable=protected-access
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", _write_config(tmp_path, ONE_DEVICE)])
    saved_handler = signal.getsignal(signal.SIGTERM)
    threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGTERM)).start()
    try:
        assert main_mod.main() == main_mod.EXIT_SUCCESS
    finally:
        signal.signal(signal.SIGTERM, saved_handler)
    assert DownBrokerClient.instances[-1].stopped
    assert "retained topics cannot be removed" in caplog.text
