"""
Daemon entry-point unit tests: a broken config ends with the NOTCONFIGURED
exit code, the journald detection does not misfire, and the control-publishing
helpers put out the address indicator and availability.
"""

import json
from types import SimpleNamespace

from wb.dauerhaft_pro import main as main_mod
from wb.dauerhaft_pro.mqtt import WbDevice


class _RecordingClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, value, retain=False):
        self.published.append((topic, value))


def test_broken_config_returns_notconfigured(tmp_path, monkeypatch):
    """
    A broken config ends the daemon with exit code 6 (NOTCONFIGURED), no traceback.
    """
    conf = tmp_path / "bad.conf"
    conf.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", str(conf)])
    assert main_mod.main() == main_mod.EXIT_CONFIG_ERROR


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


def test_build_controls_and_publish_state():
    """
    build_controls publishes the read-only address indicator; publish_state
    publishes the current address and availability (online -> empty
    /meta/error, offline -> "r").
    """
    client = _RecordingClient()
    dev = WbDevice(client, "dauerhaft_5f", "Привод")
    act = SimpleNamespace(online=True, cfg=SimpleNamespace(address=0x5F))

    main_mod.build_controls(dev, act)
    main_mod.publish_state(dev, act)
    published = dict(client.published)
    assert published["/devices/dauerhaft_5f/controls/address"] == "0x5F"
    meta = json.loads(published["/devices/dauerhaft_5f/controls/address/meta"])
    assert meta["readonly"] is True and meta["order"] == 5
    assert published["/devices/dauerhaft_5f/meta/error"] == ""  # online

    act.online = False
    main_mod.publish_state(dev, act)
    assert dict(client.published)["/devices/dauerhaft_5f/meta/error"] == "r"  # offline
