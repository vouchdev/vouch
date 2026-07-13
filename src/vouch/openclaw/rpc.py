"""JSONL-style RPC bridge for the OpenClaw JS plugin entry."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

from .context_engine import create_vouch_context_engine
from .types import CompactParams

_METHODS = frozenset({"ingest", "assemble", "compact", "info"})


def _json_default(obj: Any) -> Any:
    """Serialize the non-JSON-native types that leak into wire payloads.

    ``contextPack.generated_at`` is a ``datetime`` — without this hook the
    assemble response crashes ``json.dumps`` on any turn where a KB was
    found, and OpenClaw quarantines the engine for the process.
    """
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _parse_envelope(raw: str) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("envelope must be a json object")
    return loaded


def _engine_from_envelope(env: dict[str, Any]):
    params = env.get("params")
    if not isinstance(params, dict):
        params = {}
    workspace = params.get("workspaceDir") or params.get("workspace_dir")
    kb_path = params.get("kbPath") or params.get("kb_path")
    agent = params.get("agent")
    project = params.get("project")
    return create_vouch_context_engine(
        kb_root=Path(kb_path) if kb_path else None,
        workspace_dir=Path(workspace) if workspace else None,
        agent=str(agent) if agent else None,
        project=str(project) if project else None,
    )


def handle_request(envelope: dict[str, Any]) -> dict[str, Any]:
    req_id = envelope.get("id")
    method = envelope.get("method")
    if method not in _METHODS:
        return {
            "id": req_id,
            "ok": False,
            "error": {"code": "unknown_method", "message": f"unknown method: {method!r}"},
        }
    try:
        engine = _engine_from_envelope(envelope)
        params = envelope.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "info":
            result = {
                "info": {
                    "id": engine.info.id,
                    "name": engine.info.name,
                    "version": engine.info.version,
                    "ownsCompaction": engine.info.owns_compaction,
                }
            }
        elif method == "ingest":
            result = engine.ingest(params).to_wire()
        elif method == "assemble":
            result = engine.assemble(params).to_wire()
        else:
            result = engine.compact(CompactParams.from_wire(params)).to_wire()
        return {"id": req_id, "ok": True, "result": result}
    except Exception as exc:
        return {
            "id": req_id,
            "ok": False,
            "error": {"code": "engine_error", "message": str(exc)},
        }


def run_stdio() -> int:
    """Read one JSON envelope from stdin, write one response envelope to stdout."""
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stdout.write(json.dumps({"ok": False, "error": {"message": "empty stdin"}}) + "\n")
        return 1
    try:
        env = _parse_envelope(raw)
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": {"message": str(exc)}}) + "\n")
        return 1
    sys.stdout.write(json.dumps(handle_request(env), default=_json_default) + "\n")
    return 0
