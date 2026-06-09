"""Logging configuration honouring VOUCH_LOG_FORMAT.

When VOUCH_LOG_FORMAT=json, attach a JSON handler to the vouch logger
namespace so each log record is emitted as one JSON object per line with
level, logger, time, event, and any structured extras passed by callers
(notably actor and object_ids, to mirror the audit-log shape).

Any other value (including unset) is a no-op: vouch loggers keep stdlib
default behaviour, so this module never changes log routing for callers
who have not opted in.

Entry points (cli.cli, server.run_stdio, jsonl_server.run_jsonl) call
configure_logging() exactly once at startup. The implementation is
idempotent — repeated calls replace the existing handler rather than
stacking new ones.

Environment variables honoured:

  VOUCH_LOG_FORMAT   "text" (default) or "json"
  VOUCH_LOG_LEVEL    Any standard level name (default: WARNING).
                     Applied to the vouch logger namespace.
  VOUCH_LOG_FILE     Optional filesystem path. When set, logs are also
                     appended to this file in addition to stderr.
                     Honoured for both text and json formats.
                     A bad path (e.g. read-only FS) is caught and a
                     warning is emitted to stderr instead of crashing.

Sensitive field redaction: top-level extra= keys whose names contain
"secret", "token", "password", or "key" are replaced with "***REDACTED***"
in JSON output so credentials are never written to log sinks.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, NamedTuple

VOUCH_LOGGER_NAME = "vouch"
ENV_VAR = "VOUCH_LOG_FORMAT"
ENV_LEVEL = "VOUCH_LOG_LEVEL"
ENV_FILE = "VOUCH_LOG_FILE"

_SENSITIVE_SUBSTRINGS = frozenset({"secret", "token", "password", "key"})

_STDLIB_RECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


def _is_sensitive(key: str) -> bool:
    """Return True if key name suggests a sensitive value."""
    lower = key.lower()
    return any(sub in lower for sub in _SENSITIVE_SUBSTRINGS)


class _VouchManagedHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker subclass for handlers configure_logging() owns.

    Identifying our handler by type (rather than tagging a plain
    StreamHandler with an attribute) keeps install/reuse/remove logic
    readable and avoids an attr-defined ignore.
    """


class JsonFormatter(logging.Formatter):
    """Emit each log record as one JSON object per line.

    Always includes level, logger, time, and event (the formatted message,
    overridable via extra={"event": "..."}). Structured extras attached via
    the stdlib extra= parameter are merged into the same object.

    Sensitive fields (keys containing secret, token, password, or key) are
    replaced with ***REDACTED*** so credentials never reach log sinks.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "logger": record.name,
            "event": (
                record.__dict__["event"]
                if "event" in record.__dict__
                else record.getMessage()
            ),
        }
        for key, value in record.__dict__.items():
            if key in _STDLIB_RECORD_FIELDS or key in payload:
                continue
            payload[key] = "***REDACTED***" if _is_sensitive(key) else value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


class LoggingConfig(NamedTuple):
    """Result of configure_logging() — effective format, level, and file path."""
    format: str
    level: int
    log_file: str | None


def _selected_format() -> str:
    """Return "json" iff VOUCH_LOG_FORMAT env var is "json", else "text"."""
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    return "json" if raw == "json" else "text"


def _open_log_file(path: str) -> _VouchManagedHandler | None:
    """Open a file handler for path, returning None and warning on OSError."""
    try:
        return _VouchManagedHandler(open(path, "a", encoding="utf-8"))
    except OSError as exc:
        print(
            f"vouch: warning: could not open VOUCH_LOG_FILE {path!r}: {exc}",
            file=sys.stderr,
        )
        return None


def configure_logging(*, force: bool = False) -> LoggingConfig:
    """Install or update the vouch log handler based on environment variables.

    In json mode: attaches a stderr _VouchManagedHandler with JsonFormatter
    to the vouch logger and sets propagate=False so vouch records do not
    double-emit through the root handler. In text mode (the default): vouch
    loggers propagate to the root handler as normal — no managed handler is
    added unless VOUCH_LOG_FILE is set, in which case a file handler is
    installed so file output works without requiring json mode.

    If VOUCH_LOG_FILE is set, a _VouchManagedHandler writing to that file is
    also installed. A bad path emits a warning to stderr and is skipped rather
    than crashing the server.

    Safe to call from every entry point: handlers installed by a prior call
    are detected by _VouchManagedHandler type and replaced rather than
    stacked, so the function is idempotent. Pass force=True to force
    reconfiguration even if handlers are already installed (useful in tests).

    Returns a LoggingConfig named tuple reflecting the *effective* installed
    state: format and level are always read fresh from env; log_file reflects
    whether a file handler was successfully opened.
    """
    selected = _selected_format()
    level_name = os.environ.get(ENV_LEVEL, "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    log_file = os.environ.get(ENV_FILE, "").strip() or None

    logger = logging.getLogger(VOUCH_LOGGER_NAME)

    existing: list[_VouchManagedHandler] = [
        h for h in logger.handlers if isinstance(h, _VouchManagedHandler)
    ]
    if existing and not force:
        # Return the *effective* config by inspecting what is installed.
        installed_format = "json" if any(
            isinstance(h.formatter, JsonFormatter) for h in existing
        ) else "text"
        installed_file = next(
            (getattr(h.stream, "name", None) for h in existing
             if h.stream is not sys.stderr),
            None,
        )
        return LoggingConfig(format=installed_format, level=logger.level, log_file=installed_file)

    for h in existing:
        logger.removeHandler(h)
        h.close()

    def _make_formatter() -> logging.Formatter:
        if selected == "json":
            return JsonFormatter()
        return logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    effective_log_file: str | None = None

    if selected == "json":
        stderr_handler = _VouchManagedHandler(sys.stderr)
        stderr_handler.setFormatter(_make_formatter())
        logger.addHandler(stderr_handler)
        if log_file:
            fh = _open_log_file(log_file)
            if fh is not None:
                fh.setFormatter(_make_formatter())
                logger.addHandler(fh)
                effective_log_file = log_file
        logger.propagate = False
    else:
        # Text mode is a true no-op for the vouch logger unless VOUCH_LOG_FILE
        # is set — propagate=True lets the root handler do its job as before.
        if log_file:
            fh = _open_log_file(log_file)
            if fh is not None:
                fh.setFormatter(_make_formatter())
                logger.addHandler(fh)
                effective_log_file = log_file
        logger.propagate = True

    logger.setLevel(level)
    return LoggingConfig(format=selected, level=level, log_file=effective_log_file)
