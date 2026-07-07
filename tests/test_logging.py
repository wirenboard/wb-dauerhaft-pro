"""Unit tests for the logging setup (journal detection, handler selection).

These are pure: the real journal behaviour (Python log level -> journal PRIORITY)
is verified live on a controller, since it needs systemd. Here we only check the
selection logic, with systemd faked, so it runs anywhere including CI.
"""

import logging
import sys
import types

from wb_dauerhaft_pro import main


def test_stderr_goes_to_journal_false_without_env(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    assert main._stderr_goes_to_journal() is False


def test_stderr_goes_to_journal_false_on_malformed_env(monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "not-a-device-inode")
    assert main._stderr_goes_to_journal() is False


def test_stderr_goes_to_journal_true_when_stream_matches(monkeypatch):
    fake_stat = types.SimpleNamespace(st_dev=42, st_ino=1234)
    monkeypatch.setenv("JOURNAL_STREAM", "42:1234")
    monkeypatch.setattr(main.os, "fstat", lambda _fd: fake_stat)
    assert main._stderr_goes_to_journal() is True


def test_stderr_goes_to_journal_false_when_stream_differs(monkeypatch):
    fake_stat = types.SimpleNamespace(st_dev=42, st_ino=1234)
    monkeypatch.setenv("JOURNAL_STREAM", "42:9999")
    monkeypatch.setattr(main.os, "fstat", lambda _fd: fake_stat)
    assert main._stderr_goes_to_journal() is False


def test_make_log_handler_console_when_not_journal(monkeypatch):
    monkeypatch.setattr(main, "_stderr_goes_to_journal", lambda: False)
    handler = main._make_log_handler()
    assert isinstance(handler, logging.StreamHandler)


def test_make_log_handler_uses_journal_when_available(monkeypatch):
    monkeypatch.setattr(main, "_stderr_goes_to_journal", lambda: True)

    created = {}

    class FakeJournalHandler:
        def __init__(self, **kwargs):
            created.update(kwargs)

    fake_journal = types.ModuleType("systemd.journal")
    fake_journal.JournalHandler = FakeJournalHandler
    fake_systemd = types.ModuleType("systemd")
    fake_systemd.journal = fake_journal
    monkeypatch.setitem(sys.modules, "systemd", fake_systemd)
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal)

    handler = main._make_log_handler()
    assert isinstance(handler, FakeJournalHandler)
    assert created.get("SYSLOG_IDENTIFIER") == "wb-dauerhaft-pro"


def test_make_log_handler_falls_back_when_systemd_missing(monkeypatch):
    monkeypatch.setattr(main, "_stderr_goes_to_journal", lambda: True)
    # A None entry in sys.modules makes ``import systemd.journal`` raise ImportError.
    monkeypatch.setitem(sys.modules, "systemd.journal", None)
    handler = main._make_log_handler()
    assert isinstance(handler, logging.StreamHandler)


def test_setup_logging_attaches_single_handler(monkeypatch):
    monkeypatch.setattr(main, "_stderr_goes_to_journal", lambda: False)
    root = logging.getLogger()
    saved, saved_level = root.handlers[:], root.level
    try:
        main._setup_logging(False)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert root.level == logging.INFO
        # Idempotent: a second call must replace, not stack, handlers.
        main._setup_logging(True)
        assert len(root.handlers) == 1
        assert root.level == logging.DEBUG
    finally:
        root.handlers[:] = saved
        root.setLevel(saved_level)


def test_setup_logging_journal_handler_stays_formatter_less(monkeypatch):
    monkeypatch.setattr(main, "_stderr_goes_to_journal", lambda: True)

    class FakeJournalHandler(logging.Handler):
        def __init__(self, **_kwargs):
            super().__init__()

        def emit(self, record):
            pass

    fake_journal = types.ModuleType("systemd.journal")
    fake_journal.JournalHandler = FakeJournalHandler
    fake_systemd = types.ModuleType("systemd")
    fake_systemd.journal = fake_journal
    monkeypatch.setitem(sys.modules, "systemd", fake_systemd)
    monkeypatch.setitem(sys.modules, "systemd.journal", fake_journal)

    root = logging.getLogger()
    saved, saved_level = root.handlers[:], root.level
    try:
        main._setup_logging(False)
        assert len(root.handlers) == 1
        # Formatter-less => the journal gets a clean message + PRIORITY, not
        # "LEVEL:name:message" (the basicConfig(handlers=...) trap).
        assert root.handlers[0].formatter is None
    finally:
        root.handlers[:] = saved
        root.setLevel(saved_level)
