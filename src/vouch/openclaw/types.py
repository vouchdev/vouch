"""OpenClaw context-engine wire types.

These mirror the ContextEngine interface documented at
https://docs.openclaw.ai/concepts/context-engine. They are intentionally
SDK-free so the engine can be unit-tested without the OpenClaw host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ContextEngineInfo:
    """Static identity returned by ``VouchContextEngine.info``."""

    id: str
    name: str
    version: str
    owns_compaction: bool = False


@dataclass
class AgentMessage:
    """Minimal message shape OpenClaw passes into ingest/assemble."""

    role: str
    content: str | list[dict[str, Any]] | Any = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AgentMessage:
        role = str(raw.get("role", ""))
        content = raw.get("content", "")
        known = {"role", "content"}
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(role=role, content=content, extra=extra)


@dataclass
class IngestParams:
    session_id: str
    message: AgentMessage
    is_heartbeat: bool = False


@dataclass
class IngestResult:
    ingested: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {"ingested": self.ingested}


@dataclass
class AssembleParams:
    session_id: str
    messages: list[AgentMessage] = field(default_factory=list)
    token_budget: int | None = None
    session_key: str | None = None
    available_tools: set[str] | None = None
    citations_mode: str | None = None
    model: str | None = None
    prompt: str | None = None
    project: str | None = None
    agent: str | None = None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> AssembleParams:
        messages = [
            AgentMessage.from_wire(m)
            if isinstance(m, dict)
            else AgentMessage(role="", content=str(m))
            for m in raw.get("messages") or []
        ]
        tools_raw = raw.get("availableTools") or raw.get("available_tools")
        tools: set[str] | None
        if tools_raw is None:
            tools = None
        elif isinstance(tools_raw, (list, set, tuple)):
            tools = {str(t) for t in tools_raw}
        else:
            tools = None
        budget = raw.get("tokenBudget", raw.get("token_budget"))
        token_budget = int(budget) if budget is not None else None
        return cls(
            session_id=str(raw.get("sessionId") or raw.get("session_id") or ""),
            messages=messages,
            token_budget=token_budget,
            session_key=raw.get("sessionKey") or raw.get("session_key"),
            available_tools=tools,
            citations_mode=raw.get("citationsMode") or raw.get("citations_mode"),
            model=raw.get("model"),
            prompt=raw.get("prompt"),
            project=raw.get("project"),
            agent=raw.get("agent"),
        )


@dataclass
class AssembleResult:
    messages: list[AgentMessage]
    estimated_tokens: int
    system_prompt_addition: str | None = None
    context_pack: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "messages": [
                {"role": m.role, "content": m.content, **m.extra}
                for m in self.messages
            ],
            "estimatedTokens": self.estimated_tokens,
        }
        if self.system_prompt_addition:
            out["systemPromptAddition"] = self.system_prompt_addition
        if self.context_pack is not None:
            out["contextPack"] = self.context_pack
        if self.meta:
            out["_meta"] = self.meta
        return out


@dataclass
class CompactParams:
    session_id: str
    session_file: str = ""
    token_budget: int | None = None
    force: bool = False

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> CompactParams:
        budget = raw.get("tokenBudget", raw.get("token_budget"))
        return cls(
            session_id=str(raw.get("sessionId") or raw.get("session_id") or ""),
            session_file=str(raw.get("sessionFile") or raw.get("session_file") or ""),
            token_budget=int(budget) if budget is not None else None,
            force=bool(raw.get("force", False)),
        )


CompactReason = Literal["delegated", "no-runtime", "disabled"]


@dataclass
class CompactResult:
    ok: bool = True
    compacted: bool = False
    reason: CompactReason | str = "delegated"

    def to_wire(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "compacted": self.compacted,
            "reason": self.reason,
        }
