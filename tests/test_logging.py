"""Tests for VOUCH_LOG_FORMAT=json structured logging."""

from __future__ import annotations

import io
import json
import logging

import pytest

from vouch.logging_config import (
    VOUCH_LOGGER_NAME,
    JsonFormatter,
    _VouchManagedHandler,
    configure_logging,
)


@pytest.fixture(autouse=True)
def _reset_vouch_logger(monkeypatch: pytest.MonkeyPatch):
    """Snapshot and restore the `vouch` logger so tests stay isolated.

    `configure_logging()` mutates the process-wide `vouch` logger
    (handlers, propagation, level). Without this fixture a test that
    flips into `json` mode would leak its handler into every subsequent
    test in the run.
    """
    monkeypatch.delenv("VOUCH_LOG_FORMAT", raising=False)
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    before_handlers = list(logger.handlers)
    before_propagate = logger.propagate
    before_level = logger.level
    yield
    for h in list(logger.handlers):
        if h not in before_handlers:
            logger.removeHandler(h)
    logger.propagate = before_propagate
    logger.setLevel(before_level)


def _emit(record_kwargs: dict | None = None, level: int = logging.INFO) -> str:
    """Drive a record through JsonFormatter and return the serialized line."""
    record = logging.LogRecord(
        name="vouch.test", level=level, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    for k, v in (record_kwargs or {}).items():
        setattr(record, k, v)
    return JsonFormatter().format(record)


def test_json_formatter_emits_required_fields():
    line = _emit()
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "vouch.test"
    assert payload["event"] == "hello world"


def test_json_formatter_merges_extra_fields():
    line = _emit({"actor": "alice", "object_ids": ["claim-1", "claim-2"]})
    payload = json.loads(line)
    assert payload["actor"] == "alice"
    assert payload["object_ids"] == ["claim-1", "claim-2"]


def test_json_formatter_event_override_wins_over_message():
    line = _emit({"event": "kb.approve"})
    payload = json.loads(line)
    assert payload["event"] == "kb.approve"


def test_json_formatter_event_override_allows_falsy_values():
    for falsy in ("", 0, False):
        payload = json.loads(_emit({"event": falsy}))
        assert payload["event"] == falsy, (
            f"expected explicit falsy event {falsy!r} to be preserved, "
            f"got {payload['event']!r}"
        )


def test_json_formatter_includes_exception_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="vouch.test", level=logging.ERROR, pathname=__file__,
            lineno=1, msg="oops", args=(), exc_info=sys.exc_info(),
        )
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert "ValueError: boom" in payload["exc"]


def test_configure_logging_no_env_var_is_noop():
    selected = configure_logging()
    assert selected == "text"
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    assert not any(isinstance(h, _VouchManagedHandler) for h in logger.handlers)
    assert logger.propagate is True


@pytest.mark.parametrize("value", ["", "text"])
def test_configure_logging_explicit_text_values_are_noop(monkeypatch, value):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", value)
    selected = configure_logging()
    assert selected == "text"
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    assert not any(isinstance(h, _VouchManagedHandler) for h in logger.handlers)
    assert logger.propagate is True


def test_configure_logging_json_installs_handler(monkeypatch):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    selected = configure_logging()
    assert selected == "json"
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    managed = [h for h in logger.handlers if isinstance(h, _VouchManagedHandler)]
    assert len(managed) == 1
    assert isinstance(managed[0].formatter, JsonFormatter)
    assert logger.propagate is False


def test_configure_logging_is_idempotent(monkeypatch):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    configure_logging()
    configure_logging()
    configure_logging()
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    managed = [h for h in logger.handlers if isinstance(h, _VouchManagedHandler)]
    assert len(managed) == 1


def test_configure_logging_switches_back_to_text(monkeypatch):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    configure_logging()
    monkeypatch.delenv("VOUCH_LOG_FORMAT", raising=False)
    selected = configure_logging()
    assert selected == "text"
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    assert not any(isinstance(h, _VouchManagedHandler) for h in logger.handlers)


def test_json_handler_emits_one_object_per_line(monkeypatch):
    monkeypatch.setenv("VOUCH_LOG_FORMAT", "json")
    configure_logging()
    logger = logging.getLogger(VOUCH_LOGGER_NAME)
    handler = next(h for h in logger.handlers if isinstance(h, _VouchManagedHandler))
    buf = io.StringIO()
    handler.stream = buf
    logger.setLevel(logging.INFO)

    child = logging.getLogger("vouch.sessions")
    child.info(
        "session.crystallize",
        extra={"event": "session.crystallize", "actor": "alice",
               "object_ids": ["sess-1", "claim-2"]},
    )
    child.warning(
        "approval failed",
        extra={"event": "proposal.approve_failed", "actor": "bob",
               "object_ids": ["prop-9"]},
    )
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "actor": "alice",
        "event": "session.crystallize",
        "level": "INFO",
        "logger": "vouch.sessions",
        "object_ids": ["sess-1", "claim-2"],
    }
    second = json.loads(lines[1])
    assert second["level"] == "WARNING"
    assert second["event"] == "proposal.approve_failed"
