"""Scoped credentials — issue #608."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import agents, jsonl_server, scopes, trust
from vouch.capabilities import METHODS, capabilities
from vouch.scopes import ADMIN, APPROVE, PROPOSE, READ, ScopeError
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    kb = KBStore.init(tmp_path)
    monkeypatch.chdir(kb.root)
    return kb


def _trust(*granted: str) -> trust.VouchTrust:
    return trust.VouchTrust(
        remote=True, caller_kind="jsonl_http", auth_subject="abc",
        scopes=tuple(granted),
    )


def _call(method: str, params: dict | None = None) -> dict:
    return jsonl_server.handle_request(
        {"id": "1", "method": method, "params": params or {}}
    )


def _code(method: str, params: dict | None = None) -> str:
    res = _call(method, params)
    return "ok" if res["ok"] else res["error"]["code"]


# --- the classification table --------------------------------------------


def test_every_method_is_classified() -> None:
    """The guard the issue asks for.

    Without this a newly-added kb.* method silently lands in no scope —
    unreachable for every scoped caller — or, if the default were permissive,
    in all of them.
    """
    unclassified = [m for m in METHODS if m not in scopes.METHOD_SCOPES]
    assert not unclassified, f"methods with no scope: {unclassified}"


def test_the_table_has_no_entries_for_methods_that_do_not_exist() -> None:
    stale = [m for m in scopes.METHOD_SCOPES if m not in METHODS]
    assert not stale, f"scope entries for unknown methods: {stale}"


def test_every_scope_is_a_known_scope() -> None:
    assert set(scopes.METHOD_SCOPES.values()) <= set(scopes.ALL_SCOPES)


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("kb.search", READ),
        ("kb.context", READ),
        ("kb.audit", READ),
        ("kb.propose_claim", PROPOSE),
        ("kb.register_source", PROPOSE),
        ("kb.compile", PROPOSE),
        ("kb.approve", APPROVE),
        ("kb.supersede", APPROVE),
        ("kb.archive", APPROVE),
        ("kb.clear_claims", ADMIN),
        ("kb.index_rebuild", ADMIN),
    ],
)
def test_representative_methods_land_in_the_right_bucket(
    method: str, expected: str
) -> None:
    assert scopes.scope_for_method(method) == expected


def test_approve_is_not_reachable_from_the_default_scopes() -> None:
    """Withholding kb:approve by default *is* the review gate."""
    assert scopes.DEFAULT_SCOPES == (READ, PROPOSE)
    assert scopes.permits(scopes.DEFAULT_SCOPES, "kb.approve") is False
    assert scopes.permits(scopes.DEFAULT_SCOPES, "kb.propose_claim") is True


# --- permits --------------------------------------------------------------


def test_unscoped_permits_everything(store: KBStore) -> None:
    """The back-compat rule: a pre-#608 token keeps every power it had."""
    for method in METHODS:
        assert scopes.permits((), method) is True
        assert scopes.permits(None, method) is True


def test_an_unclassified_method_is_denied_to_a_scoped_caller() -> None:
    """Fails closed: forgetting a table entry costs a method, not the gate."""
    assert scopes.permits((READ,), "kb.brand_new_method") is False
    assert scopes.permits((), "kb.brand_new_method") is True


def test_methods_for_lists_only_what_is_allowed() -> None:
    read_only = scopes.methods_for((READ,))
    assert "kb.search" in read_only
    assert "kb.approve" not in read_only
    assert "kb.propose_claim" not in read_only
    assert len(scopes.methods_for(())) == len(METHODS)


def test_describe_reports_the_caller_contract() -> None:
    block = scopes.describe((READ,))
    assert block["available"] == list(scopes.ALL_SCOPES)
    assert block["granted"] == [READ]
    assert block["unscoped"] is False
    assert "kb.approve" not in block["allowed_methods"]

    unscoped = scopes.describe(())
    assert unscoped["unscoped"] is True


# --- parse_scopes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ()),
        ("", ()),
        ("kb:read", (READ,)),
        ("kb:read,kb:propose", (READ, PROPOSE)),
        (" kb:read , kb:propose ", (READ, PROPOSE)),
        ("kb:read,kb:read", (READ,)),
        (["kb:read", "kb:approve"], (READ, APPROVE)),
    ],
)
def test_parse_scopes_normalises(raw: object, expected: tuple[str, ...]) -> None:
    assert scopes.parse_scopes(raw) == expected  # type: ignore[arg-type]


def test_parse_scopes_rejects_an_unknown_scope() -> None:
    """A typo must not silently mint a credential with the wrong powers."""
    with pytest.raises(ScopeError, match="unknown scope"):
        scopes.parse_scopes("kb:read,kb:destroy")


# --- enforcement at the jsonl / http dispatch ----------------------------


def test_read_only_credential_is_denied_the_write_paths(store: KBStore) -> None:
    with trust.trust_context(_trust(READ)):
        assert _code("kb.status") == "ok"
        assert _code("kb.propose_claim", {"text": "x", "evidence": []}) == (
            "permission_denied"
        )
        assert _code("kb.approve", {"proposal_id": "x", "approved_by": "y"}) == (
            "permission_denied"
        )
        assert _code("kb.clear_claims") == "permission_denied"


def test_default_credential_may_propose_but_not_approve(store: KBStore) -> None:
    with trust.trust_context(_trust(*scopes.DEFAULT_SCOPES)):
        # reaches the handler (its own validation error), i.e. scope allowed it
        assert _code("kb.propose_claim", {"text": "x", "evidence": []}) != (
            "permission_denied"
        )
        assert _code("kb.approve", {"proposal_id": "x", "approved_by": "y"}) == (
            "permission_denied"
        )


def test_unscoped_credential_reaches_every_handler(store: KBStore) -> None:
    with trust.trust_context(_trust()):
        assert _code("kb.status") == "ok"
        assert _code("kb.clear_claims") == "ok"
        assert _code("kb.approve", {"proposal_id": "x", "approved_by": "y"}) != (
            "permission_denied"
        )


def test_denial_names_the_scope_it_needed(store: KBStore) -> None:
    with trust.trust_context(_trust(READ)):
        res = _call("kb.approve", {"proposal_id": "x", "approved_by": "y"})
    assert res["error"]["message"] == (
        "kb.approve requires kb:approve; this credential holds kb:read"
    )


def test_denial_happens_before_the_handler_runs(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused call must not have side effects on its way to being refused."""
    called: list[str] = []
    real = jsonl_server.HANDLERS["kb.clear_claims"]

    def spy(params: dict) -> dict:
        called.append("ran")
        return real(params)

    monkeypatch.setitem(jsonl_server.HANDLERS, "kb.clear_claims", spy)
    with trust.trust_context(_trust(READ)):
        assert _code("kb.clear_claims") == "permission_denied"
    assert called == []


def test_unknown_method_still_reports_method_not_found(store: KBStore) -> None:
    """Scope checking must not turn a typo into a permission error."""
    with trust.trust_context(_trust(READ)):
        assert _code("kb.no_such_method") == "method_not_found"


# --- enforcement on the mcp surface --------------------------------------


def test_tool_name_maps_to_its_method() -> None:
    assert trust.method_name_for_tool("kb_search") == "kb.search"
    assert trust.method_name_for_tool("kb_read_page") == "kb.read_page"
    assert trust.method_name_for_tool("kb_explain_ranking") == "kb.explain_ranking"


def test_wrapped_mcp_tool_enforces_scope(store: KBStore) -> None:
    def kb_approve(**kwargs: object) -> dict:
        return {"approved": True}

    wrapped = trust.wrap_tool_fn(kb_approve)
    with trust.trust_context(_trust(READ)), pytest.raises(
        trust.ScopeDenied, match="requires kb:approve"
    ):
        wrapped()
    with trust.trust_context(_trust(APPROVE)):
        assert wrapped()["approved"] is True
    with trust.trust_context(_trust()):
        assert wrapped()["approved"] is True


def test_wrapped_async_mcp_tool_enforces_scope(store: KBStore) -> None:
    """The async branch needs the same guard as the sync one."""
    import asyncio

    async def kb_approve(**kwargs: object) -> dict:
        return {"approved": True}

    wrapped = trust.wrap_tool_fn(kb_approve)
    with trust.trust_context(_trust(READ)), pytest.raises(trust.ScopeDenied):
        asyncio.run(wrapped())
    with trust.trust_context(_trust(APPROVE)):
        assert asyncio.run(wrapped())["approved"] is True


# --- the trust block + capabilities --------------------------------------


def test_scopes_appear_in_the_trust_block_only_when_scoped() -> None:
    assert "scopes" not in _trust().as_meta_block()
    assert _trust(READ).as_meta_block()["scopes"] == [READ]


def test_capabilities_reports_the_callers_effective_scopes(store: KBStore) -> None:
    with trust.trust_context(_trust(READ)):
        caps = capabilities().model_dump(mode="json")
    assert caps["scopes"]["granted"] == [READ]
    assert caps["scopes"]["unscoped"] is False
    assert "kb.approve" not in caps["scopes"]["allowed_methods"]


def test_capabilities_for_an_unscoped_caller_lists_everything(
    store: KBStore
) -> None:
    with trust.trust_context(_trust()):
        caps = capabilities().model_dump(mode="json")
    assert caps["scopes"]["unscoped"] is True
    assert len(caps["scopes"]["allowed_methods"]) == len(METHODS)


# --- the registry supplies the scopes ------------------------------------


def test_registered_scopes_are_resolved_for_a_subject(store: KBStore) -> None:
    agents.register(
        store, subject="abc123", name="ci-bot", actor="human",
        scopes=(READ,),
    )
    assert agents.scopes_for_subject(store, "abc123") == (READ,)


def test_an_unregistered_subject_is_unscoped(store: KBStore) -> None:
    """Back-compat again: an unknown token keeps every power."""
    assert agents.scopes_for_subject(store, "never-registered") == ()


def test_registering_an_unknown_scope_is_refused(store: KBStore) -> None:
    with pytest.raises(agents.AgentError, match="unknown scope"):
        agents.register(
            store, subject="abc123", name="ci-bot", actor="human",
            scopes=("kb:destroy",),
        )


def test_subject_scopes_is_unscoped_without_a_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert agents.subject_scopes("whatever") == ()


def test_subject_scopes_reads_the_discovered_registry(store: KBStore) -> None:
    agents.register(
        store, subject="abc123", name="ci-bot", actor="human", scopes=(READ,),
    )
    assert agents.subject_scopes("abc123") == (READ,)


def test_cli_registers_a_read_only_agent(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    res = CliRunner().invoke(cli, [
        "agents", "register", "reader", "--subject", "abc123", "--scope", READ,
    ])
    assert res.exit_code == 0, res.output
    assert agents.scopes_for_subject(store, "abc123") == (READ,)


def test_cli_rejects_an_unknown_scope_cleanly(store: KBStore) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    res = CliRunner().invoke(cli, [
        "agents", "register", "reader", "--subject", "abc123",
        "--scope", "kb:destroy",
    ])
    assert res.exit_code != 0
    assert "unknown scope" in res.output
    assert "Traceback" not in res.output


# --- the mcp-over-http middleware carries scopes through -----------------


def test_mcp_http_middleware_applies_registry_scopes(store: KBStore) -> None:
    """The ASGI wrapper must resolve scopes, not just the subject.

    This is the path a scoped MCP-over-HTTP agent actually authenticates on;
    without it a read-only credential would arrive unscoped and hold
    everything.
    """
    import asyncio

    from vouch.http_server import _McpTrustASGI

    token = "mcp-token-example"
    subject = trust.auth_subject_for_token(token)
    agents.register(
        store, subject=subject, name="mcp-reader", actor="human", scopes=(READ,),
    )

    seen: list[trust.VouchTrust] = []

    async def inner(scope: dict, receive: object, send: object) -> None:
        seen.append(trust.current())

    app = _McpTrustASGI(inner, accepted=(token,))
    asyncio.run(app(
        {"type": "http",
         "headers": [(b"authorization", f"Bearer {token}".encode())]},
        None, None,
    ))

    assert len(seen) == 1
    assert seen[0].auth_subject == subject
    assert seen[0].scopes == (READ,)
    assert seen[0].permits("kb.search") is True
    assert seen[0].permits("kb.approve") is False


def test_mcp_http_middleware_passes_non_http_scopes_through(
    store: KBStore
) -> None:
    import asyncio

    from vouch.http_server import _McpTrustASGI

    calls: list[str] = []

    async def inner(scope: dict, receive: object, send: object) -> None:
        calls.append(scope["type"])

    app = _McpTrustASGI(inner, accepted=("t",))
    asyncio.run(app({"type": "lifespan"}, None, None))
    assert calls == ["lifespan"]


def test_mcp_http_middleware_leaves_an_unauthenticated_call_unscoped(
    store: KBStore
) -> None:
    import asyncio

    from vouch.http_server import _McpTrustASGI

    seen: list[trust.VouchTrust] = []

    async def inner(scope: dict, receive: object, send: object) -> None:
        seen.append(trust.current())

    app = _McpTrustASGI(inner, accepted=("t",))
    asyncio.run(app({"type": "http", "headers": []}, None, None))
    assert seen[0].auth_subject is None
    assert seen[0].scopes == ()
