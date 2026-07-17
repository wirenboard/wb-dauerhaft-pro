"""
Daemon entry-point unit tests: a broken config must end with the
NOTCONFIGURED exit code, and the journald detection must not misfire.

The module under test imports the whole package; until every sibling module
is present in the tree these tests skip instead of breaking collection.
"""

import pytest

main_mod = pytest.importorskip("wb.dauerhaft_pro.main")


def test_broken_config_returns_notconfigured(tmp_path, monkeypatch):
    conf = tmp_path / "bad.conf"
    conf.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", str(conf)])
    assert main_mod.main() == main_mod.EXIT_CONFIG_ERROR


def test_journal_detection_rejects_foreign_streams(monkeypatch):
    # the detector is internal by design, but its parsing must be pinned:
    # a false positive would silence console logs
    # pylint: disable=protected-access
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert main_mod._stderr_goes_to_journal() is False  # not under systemd
    monkeypatch.setenv("JOURNAL_STREAM", "not:numbers")
    assert main_mod._stderr_goes_to_journal() is False  # malformed value
