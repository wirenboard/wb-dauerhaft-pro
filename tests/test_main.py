"""
Daemon entry-point unit tests: a broken config must end with the
NOTCONFIGURED exit code, and the journald detection must not misfire.
"""

from wb.dauerhaft_pro import main as main_mod


def test_broken_config_returns_notconfigured(tmp_path, monkeypatch):
    """
    Битый конфиг завершает демона кодом 6 (NOTCONFIGURED), без трейсбека.
    """
    conf = tmp_path / "bad.conf"
    conf.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["wb-dauerhaft-pro", "-c", str(conf)])
    assert main_mod.main() == main_mod.EXIT_CONFIG_ERROR


def test_journal_detection_rejects_foreign_streams(monkeypatch):
    """
    Определение журнального потока не включается без `$JOURNAL_STREAM` и на
    мусорном значении — иначе консольные логи молча пропали бы.
    """
    # the detector is internal by design, but its parsing must be pinned
    # pylint: disable=protected-access
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert main_mod._stderr_goes_to_journal() is False  # not under systemd
    monkeypatch.setenv("JOURNAL_STREAM", "not:numbers")
    assert main_mod._stderr_goes_to_journal() is False  # malformed value
