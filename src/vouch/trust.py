"""Trust metadata on kb.* responses (#233).

Every kb.* result carries ``_meta.vouch_trust`` so clients can see the trust
state the call was evaluated under — remote confinement, transport kind, and
an optional authenticated subject. The block is server-attached read-only
metadata (same role as gbrain's ``_meta.brain_hot_memory``): opt-in to render,
never authoritative over the KB payload.

Transport entry points set the active :class:`VouchTrust` via a
:class:`contextvars.ContextVar` before dispatch; :func:`finish_kb_result`
stamps dict-shaped results on the way out.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

CallerKind = Literal["cli", "jsonl", "jsonl_http", "mcp_stdio", "mcp_http"]

# kb.* methods whose primary job is reading durable KB state. Used by tests and
# adapters that want to assert ``_meta.vouch_trust`` is present on every read
# response over the JSONL transport.
READ_METHODS: tuple[str, ...] = (
    "kb.capabilities",
    "kb.status",
    "kb.stats",
    "kb.activity",
    "kb.search",
    "kb.context",
    "kb.read_page",
    "kb.read_claim",
    "kb.read_entity",
    "kb.read_relation",
    "kb.list_pages",
    "kb.list_claims",
    "kb.list_entities",
    "kb.list_relations",
    "kb.list_sources",
    "kb.list_pending",
    "kb.audit",
    "kb.why",
    "kb.trace",
    "kb.impact",
    "kb.graph_export",
    "kb.embeddings_stats",
    "kb.lint",
    "kb.doctor",
    "kb.export_check",
    "kb.import_check",
    "kb.volunteer_context",
)

# Minimal params that let each read handler succeed against an empty init KB.
READ_METHOD_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "kb.search": {"query": "test", "limit": 1},
    "kb.context": {"task": "test", "limit": 1},
    "kb.read_page": {"page_id": "__missing__"},
    "kb.read_claim": {"claim_id": "__missing__"},
    "kb.read_entity": {"entity_id": "__missing__"},
    "kb.read_relation": {"relation_id": "__missing__"},
    "kb.audit": {"limit": 1},
    "kb.why": {"claim_id": "__missing__"},
    "kb.trace": {"claim_id": "__missing__"},
    "kb.impact": {"claim_id": "__missing__"},
    "kb.export_check": {"bundle_path": "__missing__"},
    "kb.import_check": {"bundle_path": "__missing__"},
    "kb.volunteer_context": {"session_id": "__missing__"},
}


@dataclass(frozen=True)
class VouchTrust:
    """Resolved trust state for one kb.* call."""

    remote: bool
    caller_kind: CallerKind
    auth_subject: str | None
    # Empty means unscoped, i.e. every scope — the back-compat rule that keeps
    # tokens issued before #608 working exactly as they did.
    scopes: tuple[str, ...] = ()

    def as_meta_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {
            "remote": self.remote,
            "caller_kind": self.caller_kind,
            "auth_subject": self.auth_subject,
        }
        # Only surfaced when the credential is actually scoped, so unscoped
        # callers see the same block they saw before.
        if self.scopes:
            block["scopes"] = list(self.scopes)
        return block

    def permits(self, method: str) -> bool:
        from .scopes import permits as _permits

        return _permits(self.scopes, method)


# Presets — one per transport entry point.
CLI = VouchTrust(remote=False, caller_kind="cli", auth_subject=None)
JSONL_STDIO = VouchTrust(remote=False, caller_kind="jsonl", auth_subject=None)
JSONL_HTTP = VouchTrust(remote=True, caller_kind="jsonl_http", auth_subject=None)
MCP_STDIO = VouchTrust(remote=False, caller_kind="mcp_stdio", auth_subject=None)
MCP_HTTP = VouchTrust(remote=True, caller_kind="mcp_http", auth_subject=None)

_active: ContextVar[VouchTrust | None] = ContextVar("vouch_trust", default=None)
_stdio_default: VouchTrust = JSONL_STDIO


def set_stdio_default(trust: VouchTrust) -> None:
    """Process-wide fallback when no request-scoped trust is set (stdio MCP)."""
    global _stdio_default
    _stdio_default = trust


def current() -> VouchTrust:
    return _active.get() or _stdio_default


def reset_trust_context(token: Token) -> None:
    _active.reset(token)


def set_trust_context(trust: VouchTrust) -> Token:
    return _active.set(trust)


@contextmanager
def trust_context(trust: VouchTrust) -> Iterator[None]:
    token = set_trust_context(trust)
    try:
        yield
    finally:
        reset_trust_context(token)


def auth_subject_for_token(token: str) -> str:
    """Stable identifier for a bearer token without echoing the secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def matched_bearer_token(
    authorization: str | None,
    accepted: tuple[str, ...],
) -> str | None:
    """Return the accepted token that matched ``Authorization``, if any."""
    if not authorization or not accepted:
        return None
    for tok in accepted:
        if hmac.compare_digest(authorization, f"Bearer {tok}"):
            return tok
    return None


def authorized_bearer_token(
    authorization: str | None,
    accepted: tuple[str, ...],
    *,
    gate: Callable[[str], bool] | None = None,
) -> str | None:
    """``matched_bearer_token``, then the agent registry's paused/revoked gate.

    One chokepoint so every token transport inherits revocation rather than
    implementing it twice. ``gate`` is injected rather than imported so this
    module keeps no storage dependency; ``agents.subject_is_active`` is what
    the HTTP transports pass. With no gate this is exactly the old behaviour.

    A denied token returns ``None`` — indistinguishable from a wrong token to
    the caller, which is the right shape: a paused agent learns it is not
    authenticated, not that its credential is otherwise valid.
    """
    token = matched_bearer_token(authorization, accepted)
    if token is None or gate is None:
        return token
    return token if gate(auth_subject_for_token(token)) else None


def with_auth_subject(
    trust: VouchTrust, token: str | None, *, scopes: tuple[str, ...] = ()
) -> VouchTrust:
    if token is None:
        return trust
    return VouchTrust(
        remote=trust.remote,
        caller_kind=trust.caller_kind,
        auth_subject=auth_subject_for_token(token),
        scopes=scopes,
    )


class ScopeDenied(PermissionError):
    """The active credential's scopes do not cover this method."""


def require_scope(method: str) -> None:
    """Raise :class:`ScopeDenied` when the active credential may not call it.

    One check, called from both dispatch points, so MCP / JSONL / HTTP inherit
    scoping from the same place rather than three near-identical guards.
    """
    from .scopes import scope_for_method

    trust = current()
    if trust.permits(method):
        return
    required = scope_for_method(method)
    detail = (
        f"requires {required}" if required
        else "is not classified under any scope"
    )
    raise ScopeDenied(
        f"{method} {detail}; this credential holds "
        f"{', '.join(trust.scopes) or 'no scopes'}"
    )


def attach_trust(result: dict[str, Any]) -> dict[str, Any]:
    """Attach ``_meta.vouch_trust`` to a dict-shaped kb.* result."""
    result.setdefault("_meta", {})["vouch_trust"] = current().as_meta_block()
    return result


def finish_kb_result(result: Any) -> Any:
    """Stamp trust metadata on dict-shaped tool results; pass others through."""
    if isinstance(result, dict):
        return attach_trust(result)
    return result


_F = TypeVar("_F", bound=Callable[..., Any])


def method_name_for_tool(tool_name: str) -> str:
    """``kb_read_page`` -> ``kb.read_page`` — the MCP naming convention."""
    return tool_name.replace("_", ".", 1)


def wrap_tool_fn(fn: _F) -> _F:
    """Wrap a sync or async MCP tool: scope check in, trust metadata out."""
    method = method_name_for_tool(fn.__name__)

    if inspect.iscoroutinefunction(fn):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            require_scope(method)
            return finish_kb_result(await fn(*args, **kwargs))

        async_wrapper.__name__ = fn.__name__
        async_wrapper.__doc__ = fn.__doc__
        return async_wrapper  # type: ignore[return-value]

    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        require_scope(method)
        return finish_kb_result(fn(*args, **kwargs))

    sync_wrapper.__name__ = fn.__name__
    sync_wrapper.__doc__ = fn.__doc__
    return sync_wrapper  # type: ignore[return-value]


def install_mcp_trust_wrappers(mcp: Any) -> None:
    """Patch every registered FastMCP tool to stamp trust metadata on exit."""
    for tool in mcp._tool_manager.list_tools():
        wrapped = wrap_tool_fn(tool.fn)
        tool.fn = wrapped  # pydantic model allows mutation on fn field


def assert_vouch_trust(result: Any, *, expected: VouchTrust | None = None) -> None:
    """Test helper — assert ``_meta.vouch_trust`` shape (and optional exact match)."""
    assert isinstance(result, dict), f"expected dict result, got {type(result)!r}"
    meta = result.get("_meta") or {}
    block = meta.get("vouch_trust")
    assert isinstance(block, dict), "missing _meta.vouch_trust"
    assert isinstance(block.get("remote"), bool)
    assert isinstance(block.get("caller_kind"), str)
    assert block.get("auth_subject") is None or isinstance(block["auth_subject"], str)
    if expected is not None:
        assert block == expected.as_meta_block()
