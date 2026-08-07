"""
Daemon entry-point unit tests: a broken config ends with the NOTCONFIGURED exit
code, and the journald stream detection does not misfire.
"""

from types import SimpleNamespace

from wb.dauerhaft_pro import main as main_mod


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
