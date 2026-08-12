"""
Daemon entry-point unit tests: a broken config ends with the NOTCONFIGURED exit
code and a visible MQTT announcement, and the journald stream detection does
not misfire.
"""

from types import SimpleNamespace

from wb.dauerhaft_pro import main as main_mod


class FakeMessageInfo:
    """Just enough of paho's MQTTMessageInfo for the confirmation path."""

    def __init__(self):
        self.waited = False

    def wait_for_publish(self, _timeout=None):
        self.waited = True

    def is_published(self):
        return True


class FakeMQTTClient:
    """Just enough of wb_common's MQTTClient for the announcement path."""

    instances = []

    def __init__(self, client_id, broker_url=None):
        self.client_id = client_id
        self.broker_url = broker_url
        self.published = []
        self.infos = []
        self.started = False
        self.stopped = False
        FakeMQTTClient.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

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
        def start(self):
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
