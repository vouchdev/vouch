"""Tests for vouch logging configuration."""
from __future__ import annotations

import json
import logging


def _reconfigure(monkeypatch, fmt=None, level=None, log_file=None, force=True):
    """Helper: set env vars and force-reconfigure logging."""
    if fmt is None:
        monkeypatch.delenv("VOUCH_LOG_FORMAT", raising=False)
    else:
        monkeypatch.setenv("VOUCH_LOG_FORMAT", fmt)
    if level is None:
        monkeypatch.delenv("VOUCH_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("VOUCH_LOG_LEVEL", level)
    if log_file is None:
        monkeypatch.delenv("VOUCH_LOG_FILE", raising=False)
    else:
        monkeypatch.setenv("VOUCH_LOG_FILE", log_file)
    import importlib

    import vouch.logging_config as lc
    importlib.reload(lc)
    return lc.configure_logging(force=force)


def test_json_format_emits_valid_json(monkeypatch):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    from vouch.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="vouch.test", level=logging.WARNING,
        pathname="", lineno=0, msg="test message",
        args=(), exc_info=None,
    )
    line = formatter.format(record)
    obj = json.loads(line)
    assert obj["level"] == "WARNING"
    assert obj["event"] == "test message"
    assert "time" in obj
    assert "logger" in obj


def test_text_format_is_default(monkeypatch):
    cfg = _reconfigure(monkeypatch, fmt=None)
    assert cfg.format == "text"


def test_json_extra_fields_are_merged(monkeypatch):
    """Extra fields passed via extra= should appear in JSON output."""
    from vouch.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="vouch.test", level=logging.INFO,
        pathname="", lineno=0, msg="event",
        args=(), exc_info=None,
    )
    record.__dict__["proposal_id"] = "abc-123"
    record.__dict__["actor"] = "user@example.com"
    line = formatter.format(record)
    obj = json.loads(line)
    assert obj["proposal_id"] == "abc-123"
    assert obj["actor"] == "user@example.com"


def test_event_key_overridable(monkeypatch):
    """Caller can override the event field via extra={"event": "..."}."""
    from vouch.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="vouch.test", level=logging.INFO,
        pathname="", lineno=0, msg="raw message",
        args=(), exc_info=None,
    )
    record.__dict__["event"] = "custom.event.name"
    line = formatter.format(record)
    obj = json.loads(line)
    assert obj["event"] == "custom.event.name"


def test_sensitive_fields_are_redacted(monkeypatch):
    """Keys containing secret/token/password/key should be redacted in JSON."""
    from vouch.logging_config import JsonFormatter
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="vouch.test", level=logging.INFO,
        pathname="", lineno=0, msg="auth attempt",
        args=(), exc_info=None,
    )
    record.__dict__["api_token"] = "supersecret123"
    record.__dict__["password"] = "hunter2"
    record.__dict__["actor"] = "alice"
    line = formatter.format(record)
    obj = json.loads(line)
    assert obj["api_token"] == "***REDACTED***"
    assert obj["password"] == "***REDACTED***"
    assert obj["actor"] == "alice"


def test_vouch_log_level_is_honoured(monkeypatch):
    """VOUCH_LOG_LEVEL should set the vouch logger level."""
    cfg = _reconfigure(monkeypatch, fmt="json", level="DEBUG")
    logger = logging.getLogger("vouch")
    assert logger.level == logging.DEBUG
    assert cfg.level == logging.DEBUG


def test_vouch_log_file_writes_to_file(monkeypatch, tmp_path):
    """VOUCH_LOG_FILE should append log lines to the specified file."""
    log_path = tmp_path / "vouch.log"
    _reconfigure(monkeypatch, fmt="json", level="DEBUG", log_file=str(log_path))
    logger = logging.getLogger("vouch.test_file")
    logger.setLevel(logging.DEBUG)
    logger.warning("file test message")
    content = log_path.read_text()
    assert content.strip(), "Log file should not be empty"
    obj = json.loads(content.strip().splitlines()[-1])
    assert obj["event"] == "file test message"


def test_managed_handler_replaced_on_force(monkeypatch):
    """force=True should replace managed handlers, not stack them."""
    monkeypatch.delenv("VOUCH_LOG_FILE", raising=False)
    _reconfigure(monkeypatch, fmt="json")
    _reconfigure(monkeypatch, fmt="json")
    import vouch.logging_config as lc
    logger = logging.getLogger("vouch")
    managed = [h for h in logger.handlers if isinstance(h, lc._VouchManagedHandler)]
    assert len(managed) == 1


def test_returns_logging_config_namedtuple(monkeypatch):
    """configure_logging should return a LoggingConfig named tuple."""
    cfg = _reconfigure(monkeypatch, fmt="json", level="INFO")
    assert cfg.format == "json"
    assert cfg.level == logging.INFO
    assert cfg.log_file is None
    assert hasattr(cfg, "_fields")


def test_propagate_false_in_json_mode(monkeypatch):
    """vouch logger should not propagate in json mode to avoid double-emit."""
    _reconfigure(monkeypatch, fmt="json")
    logger = logging.getLogger("vouch")
    assert logger.propagate is False


def test_propagate_true_in_text_mode(monkeypatch):
    """vouch logger should propagate in text mode (stdlib default)."""
    _reconfigure(monkeypatch, fmt="text")
    logger = logging.getLogger("vouch")
    assert logger.propagate is True
