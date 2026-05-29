"""Tests for VOUCH_LOG_FORMAT=json logging configuration."""
import json
import logging


def test_json_format_emits_valid_json(monkeypatch, capsys):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    from vouch.logging_config import _JsonFormatter
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    record = logging.LogRecord(
        name="vouch.test", level=logging.WARNING,
        pathname="", lineno=0, msg="test message",
        args=(), exc_info=None,
    )
    line = handler.format(record)
    obj = json.loads(line)
    assert obj["level"] == "WARNING"
    assert obj["message"] == "test message"
    assert "time" in obj
    assert "logger" in obj


def test_text_format_is_default(monkeypatch, capsys):
    monkeypatch.delenv("VOUCH_LOG_FORMAT", raising=False)
    from vouch.logging_config import configure_logging
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        configure_logging(force=True)
        root.warning("hello text")
        captured = capsys.readouterr()
        line = captured.err.strip().splitlines()[-1] if captured.err.strip() else ""
        if line:
            try:
                json.loads(line)
                raise AssertionError("Expected non-JSON output")
            except json.JSONDecodeError:
                pass
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)
        root.setLevel(old_level)
