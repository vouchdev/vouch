"""Logging configuration for vouch.

Reads VOUCH_LOG_FORMAT at startup:
  - unset / "text" : human-readable format (default)
  - "json"         : one JSON object per log line (level, time, logger, message)

Call configure_logging() once at process startup before any log output.
"""

from __future__ import annotations

import json
import logging
import os
import sys


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            obj["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(obj, separators=(",", ":"))


def configure_logging(*, force: bool = False) -> None:
    """Configure the root logger based on VOUCH_LOG_FORMAT.

    Safe to call multiple times — subsequent calls are no-ops unless
    force=True. Call once at process startup before any log output.
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return

    fmt = os.environ.get("VOUCH_LOG_FORMAT", "text").strip().lower()
    handler = logging.StreamHandler(sys.stderr)

    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )

    root.handlers.clear()
    root.addHandler(handler)
    level = os.environ.get("VOUCH_LOG_LEVEL", "WARNING").upper()
    root.setLevel(getattr(logging, level, logging.WARNING))
