"""Logging configuration honouring `VOUCH_LOG_FORMAT`.

When `VOUCH_LOG_FORMAT=json`, attach a JSON handler to the `vouch` logger
namespace so each log record is emitted as one JSON object per line with
`level`, `logger`, `event`, and any structured extras passed by callers
(notably `actor` and `object_ids`, to mirror the audit-log shape).

Any other value (including unset) is a no-op: vouch's loggers keep the
stdlib default behaviour, so this module never changes log routing for
callers who don't opt in.

Entry points (`cli.cli`, `server.run_stdio`, `jsonl_server.run_jsonl`)
call `configure_logging()` exactly once at startup. The implementation is
idempotent — repeated calls swap the formatter on the existing handler
rather than stacking new ones.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

VOUCH_LOGGER_NAME = "vouch"
ENV_VAR = "VOUCH_LOG_FORMAT"

# stdlib LogRecord attributes — anything else on a record was attached via
# `logger.info(..., extra={"actor": ...})` and should land in JSON output.
_STDLIB_RECORD_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class _VouchManagedHandler(logging.StreamHandler):
    """Marker subclass for the handler `configure_logging()` owns.

    Identifying our handler by type (rather than tagging a plain
    `StreamHandler` with an attribute) keeps the install/reuse/remove logic
    readable and avoids an `attr-defined` ignore.
    """


class JsonFormatter(logging.Formatter):
    """Emit each log record as one JSON object per line.

    Always includes `level`, `logger`, and `event` (the formatted message,
    overridable via `extra={"event": "..."}`). Structured extras attached
    via the stdlib `extra=` parameter are merged into the same object.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise `record` as one JSON object.

        `event` defaults to the formatted message but may be overridden by
        the caller passing `extra={"event": "..."}`. All non-stdlib record
        attributes are merged in alongside the required keys. When the
        record carries `exc_info`, the formatted traceback is attached as
        `exc`.
        """
        payload: dict[str, Any] = {
            "level": record.levelname,
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
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def _selected_format() -> str:
    """Return `"json"` iff `VOUCH_LOG_FORMAT` env var is `json`, else `"text"`.

    The comparison is case- and whitespace-insensitive; any other value
    (including unset, empty, or `text`) maps to `"text"` so callers who
    have not opted in see no behaviour change.
    """
    raw = os.environ.get(ENV_VAR, "").strip().lower()
    return "json" if raw == "json" else "text"


def configure_logging() -> str:
    """Install or remove the JSON handler based on `VOUCH_LOG_FORMAT`.

    In `json` mode: attaches a stderr `StreamHandler` with `JsonFormatter`
    to the `vouch` logger and sets `propagate=False` so vouch's records do
    not double-emit through the root handler. In `text` mode: removes any
    handler this function previously installed and restores `propagate`,
    leaving the `vouch` logger in its stdlib default state.

    Safe to call from every entry point: handlers installed by a prior
    call are detected by their `_VouchManagedHandler` type and reused
    rather than stacked, so the function is idempotent across repeat
    invocations and across format switches (e.g. text -> json -> text in
    tests).

    Returns the selected format name (`"json"` or `"text"`).
    """
    selected = _selected_format()
    logger = logging.getLogger(VOUCH_LOGGER_NAME)

    existing: _VouchManagedHandler | None = next(
        (h for h in logger.handlers if isinstance(h, _VouchManagedHandler)),
        None,
    )

    if selected == "json":
        if existing is None:
            handler = _VouchManagedHandler(sys.stderr)
            logger.addHandler(handler)
        else:
            handler = existing
        handler.setFormatter(JsonFormatter())
        logger.propagate = False
    elif existing is not None:
        logger.removeHandler(existing)
        logger.propagate = True

    return selected
