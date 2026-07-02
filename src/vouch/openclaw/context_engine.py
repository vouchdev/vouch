"""Vouch OpenClaw context engine — cited KB synthesis on every assemble().

Wraps ``kb.context`` retrieval, the entity-salience reflex, and per-session hot
memory into a single ``systemPromptAddition`` block. Compaction stays with the
legacy OpenClaw runtime (``ownsCompaction: false``), matching gbrain-context's
delegation posture.

Enable in ``openclaw.json`` (the OpenClaw installer auto-binds this when it
installs a ``kind: context-engine`` plugin)::

    plugins.slots.contextEngine: "vouch"

The engine id deliberately equals the plugin id in ``openclaw.plugin.json``:
OpenClaw's installer binds the contextEngine slot to the *plugin* id, and
``resolveContextEngine`` looks the slot value up in the *engine* registry —
distinct ids would quarantine the engine as "not registered" and silently
fall back to the legacy engine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .. import hot_memory
from .. import salience as salience_mod
from ..context import build_context_pack
from ..storage import KBNotFoundError, KBStore, discover_root
from .synthesis import estimate_tokens, synthesize_context_block
from .types import (
    AgentMessage,
    AssembleParams,
    AssembleResult,
    CompactParams,
    CompactResult,
    ContextEngineInfo,
    IngestParams,
    IngestResult,
)

logger = logging.getLogger(__name__)

ENGINE_ID = "vouch"
ENGINE_NAME = "Vouch Context Engine"
ENGINE_API_VERSION = "0.1.0"

DEFAULT_ITEM_LIMIT = 12
DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_RESERVE_TOKENS = 512


def engine_info() -> ContextEngineInfo:
    return ContextEngineInfo(
        id=ENGINE_ID,
        name=ENGINE_NAME,
        version=ENGINE_API_VERSION,
        owns_compaction=False,
    )


def message_text(content: str | list[dict[str, Any]] | Any) -> str:
    """Coerce structured message content to plain text (OpenClaw block arrays)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block is not None:
                parts.append(str(block))
        return " ".join(parts)
    return str(content or "")


def last_user_text(messages: list[AgentMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return message_text(msg.content).strip()
    return ""


def resolve_task_query(params: AssembleParams) -> str:
    explicit = (params.prompt or "").strip()
    if explicit:
        return explicit
    from_messages = last_user_text(params.messages)
    if from_messages:
        return from_messages
    mem = hot_memory.get(params.session_id) if params.session_id else None
    if mem and mem.query.strip():
        return mem.query.strip()
    return ""


def resolve_kb_root(*, workspace_dir: Path | None, kb_path: str | None) -> Path:
    if kb_path:
        return Path(kb_path).expanduser().resolve()
    start = workspace_dir or Path.cwd()
    return discover_root(start)


def load_cfg(store: KBStore) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def token_budget_to_max_chars(
    token_budget: int | None,
    *,
    reserve: int = DEFAULT_RESERVE_TOKENS,
) -> int | None:
    if token_budget is None or token_budget <= 0:
        return None
    usable = max(256, token_budget - reserve)
    return usable * DEFAULT_CHARS_PER_TOKEN


def hot_memory_snapshot(session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    mem = hot_memory.get(session_id)
    if mem is None:
        return None
    return {
        "session_id": mem.session_id,
        "query": mem.query,
        "agent": mem.agent,
        "project": mem.project,
        "push_count": mem.push_count,
        "volunteered": sorted(mem.volunteered),
        "last_scores": dict(mem.last_snapshot.scores),
        "active": mem.active,
    }


def entity_name_map(store: KBStore, salience: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for rec in salience:
        eid = str(rec.get("entity_id") or "")
        if not eid:
            continue
        try:
            ent = store.get_entity(eid)
            names[eid] = ent.name or eid
        except Exception:
            names[eid] = eid
    return names


class VouchContextEngine:
    """OpenClaw-shaped context engine backed by vouch retrieval."""

    def __init__(
        self,
        *,
        kb_root: Path | None = None,
        workspace_dir: Path | None = None,
        agent: str | None = None,
        project: str | None = None,
        item_limit: int = DEFAULT_ITEM_LIMIT,
    ) -> None:
        self._kb_root = kb_root
        self._workspace_dir = workspace_dir
        self._default_agent = agent or os.environ.get("VOUCH_AGENT", "openclaw")
        self._default_project = project
        self._item_limit = max(1, item_limit)
        self.info = engine_info()

    def _store(self) -> KBStore:
        root = self._kb_root
        if root is None:
            root = resolve_kb_root(workspace_dir=self._workspace_dir, kb_path=None)
        return KBStore(root)

    def ingest(self, params: IngestParams | dict[str, Any]) -> IngestResult:
        if isinstance(params, dict):
            msg_raw = params.get("message") or {}
            message = (
                AgentMessage.from_wire(msg_raw)
                if isinstance(msg_raw, dict)
                else AgentMessage(role="user", content=str(msg_raw))
            )
            params = IngestParams(
                session_id=str(params.get("sessionId") or params.get("session_id") or ""),
                message=message,
                is_heartbeat=bool(params.get("isHeartbeat") or params.get("is_heartbeat")),
            )
        if params.is_heartbeat:
            return IngestResult(ingested=False)
        text = message_text(params.message.content).strip()
        if not params.session_id or not text:
            return IngestResult(ingested=False)
        try:
            store = self._store()
            cfg = load_cfg(store)
            enabled, window, _top_k = salience_mod.reflex_cfg(cfg)
            if enabled:
                salience_mod.record_query(params.session_id, text, window=window)
        except KBNotFoundError:
            logger.debug("ingest skipped — no KB at workspace")
            return IngestResult(ingested=False)
        except Exception:
            logger.exception("salience ingest failed for session %s", params.session_id)
            return IngestResult(ingested=False)
        return IngestResult(ingested=True)

    def assemble(self, params: AssembleParams | dict[str, Any]) -> AssembleResult:
        if isinstance(params, dict):
            params = AssembleParams.from_wire(params)
        task = resolve_task_query(params)
        agent = params.agent or self._default_agent
        project = params.project or self._default_project
        max_chars = token_budget_to_max_chars(params.token_budget)

        try:
            store = self._store()
        except KBNotFoundError:
            addition = (
                "## Vouch knowledge context\n\n"
                "_No `.vouch/` knowledge base found near the workspace. "
                "Run `vouch init` in the project root._\n"
            )
            est = estimate_tokens(*params.messages, extra_text=addition)
            return AssembleResult(
                messages=params.messages,
                estimated_tokens=est,
                system_prompt_addition=addition,
                meta={"engine": ENGINE_ID, "kb_found": False},
            )

        cfg = load_cfg(store)
        session_id = params.session_id or None
        if session_id and task:
            enabled, window, _ = salience_mod.reflex_cfg(cfg)
            if enabled:
                salience_mod.record_query(session_id, task, window=window)

        pack: dict[str, Any] = build_context_pack(  # type: ignore[assignment]
            store,
            query=task or "(no task query)",
            limit=self._item_limit,
            max_chars=max_chars,
            require_citations=False,
            project=project,
            agent=agent,
        )
        salience_mod.attach_salience(pack, store, session_id, cfg)
        salience = (pack.get("_meta") or {}).get("vouch_salience") or []
        hot = hot_memory_snapshot(session_id)
        names = entity_name_map(store, list(salience))

        addition = synthesize_context_block(
            pack=pack,
            salience=list(salience),
            hot_memory=hot,
            entity_names=names,
            citations_mode=params.citations_mode,
        )
        est = estimate_tokens(*params.messages, extra_text=addition)
        meta: dict[str, Any] = {
            "engine": ENGINE_ID,
            "engine_version": ENGINE_API_VERSION,
            "kb_found": True,
            "backend": pack.get("backend"),
            "item_count": len(pack.get("items") or []),
        }
        if hot:
            meta["vouch_hot_memory"] = hot
        if salience:
            meta["vouch_salience"] = salience

        return AssembleResult(
            messages=params.messages,
            estimated_tokens=est,
            system_prompt_addition=addition,
            context_pack=pack,
            meta=meta,
        )

    def compact(self, params: CompactParams | dict[str, Any]) -> CompactResult:
        if isinstance(params, dict):
            params = CompactParams.from_wire(params)
        # Compaction stays with the legacy runtime — same contract as gbrain-context.
        return CompactResult(ok=True, compacted=False, reason="delegated")


def create_vouch_context_engine(
    *,
    kb_root: Path | str | None = None,
    workspace_dir: Path | str | None = None,
    agent: str | None = None,
    project: str | None = None,
    item_limit: int = DEFAULT_ITEM_LIMIT,
) -> VouchContextEngine:
    """Factory used by the OpenClaw plugin entry and tests."""
    kb = Path(kb_root).expanduser().resolve() if kb_root else None
    ws = Path(workspace_dir).expanduser().resolve() if workspace_dir else None
    return VouchContextEngine(
        kb_root=kb,
        workspace_dir=ws,
        agent=agent,
        project=project,
        item_limit=item_limit,
    )


def describe_engine() -> dict[str, Any]:
    """Capability-facing engine descriptor for ``kb.capabilities``."""
    info = engine_info()
    return {
        "id": info.id,
        "name": info.name,
        "version": info.version,
        "owns_compaction": info.owns_compaction,
        "contract": "openclaw-context-engine",
        "features": [
            "cited-context-pack",
            "entity-salience-reflex",
            "hot-memory-sidebar",
            "review-gated-sources",
        ],
    }
