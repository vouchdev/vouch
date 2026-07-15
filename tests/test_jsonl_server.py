"""JSONL tool server — request/response envelope behaviour."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import health
from vouch.jsonl_server import handle_request
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_jsonl_search_request(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="findable token", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r1", "method": "kb.search",
                           "params": {"query": "findable"}})
    assert resp["ok"]
    assert resp["id"] == "r1"
    assert any(it["id"] == "c1" for it in resp["result"]["hits"])


def test_jsonl_context_coerces_string_max_chars(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    for i in range(20):
        store.put_claim(Claim(
            id=f"c{i}",
            text=f"findable claim {i} with enough padding to exceed the context budget",
            evidence=[src.id],
        ))
    health.rebuild_index(store)
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "r-context",
        "method": "kb.context",
        "params": {
            "task": "findable",
            "max_chars": "100",
            "fail_on_budget_truncation": True,
        },
    })

    assert resp["ok"]
    assert resp["result"]["quality"]["budget_truncated"] is True
    assert resp["result"]["quality"]["ok"] is False


def test_jsonl_context_rejects_invalid_string_max_chars(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)

    resp = handle_request({
        "id": "r-context",
        "method": "kb.context",
        "params": {"task": "findable", "max_chars": "many"},
    })

    assert not resp["ok"]
    assert resp["error"]["code"] == "invalid_request"
    assert "invalid literal" in resp["error"]["message"]


def test_jsonl_unknown_method_returns_error(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "r2", "method": "kb.bogus", "params": {}})
    assert not resp["ok"]
    assert resp["error"]["code"] == "method_not_found"


def test_jsonl_dry_run_propose_then_real_propose(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    monkeypatch.chdir(store.root)
    dry = handle_request({"id": "1", "method": "kb.propose_claim",
                          "params": {"text": "x", "evidence": [src.id],
                                     "dry_run": True}})
    assert dry["ok"] and dry["result"]["dry_run"] is True
    real = handle_request({"id": "2", "method": "kb.propose_claim",
                           "params": {"text": "x", "evidence": [src.id]}})
    assert real["ok"]
    pending = handle_request({"id": "3", "method": "kb.list_pending",
                              "params": {}})
    assert len(pending["result"]["items"]) == 1


def test_jsonl_full_flow(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"raw evidence")
    monkeypatch.chdir(store.root)
    pr = handle_request({"id": "1", "method": "kb.propose_claim",
                         "params": {"text": "JWT used", "evidence": [src.id]}})
    pid = pr["result"]["proposal_id"]
    monkeypatch.setenv("VOUCH_AGENT", "human-reviewer")
    handle_request({"id": "2", "method": "kb.approve",
                    "params": {"proposal_id": pid}})
    status = handle_request({"id": "3", "method": "kb.status", "params": {}})
    assert status["result"]["claims"] == 1
    caps = handle_request({"id": "4", "method": "kb.capabilities", "params": {}})
    assert caps["result"]["review_gated"] is True


def test_register_source_from_path_rejects_outside_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch
) -> None:
    # Regression for #10: kb.register_source_from_path must not read files
    # outside the project root. Without the guard, an agent can name
    # /etc/passwd, ~/.ssh/id_rsa, etc. and exfiltrate the contents via
    # kb.cite / kb.list_sources.
    kb_root = tmp_path_factory.mktemp("kb")
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("super-secret payload")
    store = KBStore.init(kb_root)
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "r1", "method": "kb.register_source_from_path",
        "params": {"path": str(secret)},
    })
    assert not resp["ok"]
    assert resp["error"]["code"] == "invalid_request"
    assert "project root" in resp["error"]["message"]
    # The store should be empty — the secret must not have been ingested.
    assert store.list_sources() == []


def test_register_source_from_path_accepts_inside_root(
    store: KBStore, monkeypatch
) -> None:
    inside = store.root / "doc.txt"
    inside.write_text("project content")
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "r1", "method": "kb.register_source_from_path",
        "params": {"path": str(inside)},
    })
    assert resp["ok"]
    assert len(store.list_sources()) == 1


def test_read_under_root_rejects_symlink_at_resolved_path(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CodeRabbit review on PR #28 flagged a TOCTOU between the containment
    # check and the actual read: even after Path.resolve() lands inside the
    # KB root, an attacker who can swap the resolved name for a symlink
    # before the read could still exfiltrate an out-of-root file. Defence
    # is O_NOFOLLOW at open time.
    #
    # Simulate the race by short-circuiting Path.resolve() so the symlink
    # itself is the "resolved" path that read_under_root opens.
    target = store.root / "real.txt"
    target.write_bytes(b"real content")
    swap_link = store.root / "swap.txt"
    swap_link.symlink_to(target)
    monkeypatch.setattr(Path, "resolve", lambda self, **_kw: self)

    with pytest.raises(ValueError, match="cannot read"):
        store.read_under_root(swap_link)


def test_read_under_root_rejects_directory(
    store: KBStore, tmp_path: Path
) -> None:
    # Without the fstat/S_ISREG check, an attacker could ingest a directory
    # listing or device node through register_source_from_path. read_under_root
    # rejects anything that isn't a regular file even when it lives inside root.
    subdir = store.root / "subdir"
    subdir.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        store.read_under_root(subdir)


def test_jsonl_session_lifecycle(store: KBStore, monkeypatch) -> None:
    src = store.put_source(b"e")
    monkeypatch.chdir(store.root)
    sess = handle_request({"id": "1", "method": "kb.session_start",
                           "params": {"task": "demo"}})
    sid = sess["result"]["id"]
    handle_request({"id": "2", "method": "kb.propose_claim",
                    "params": {"text": "x", "evidence": [src.id],
                               "session_id": sid}})
    handle_request({"id": "3", "method": "kb.session_end",
                    "params": {"session_id": sid}})
    # No agent switch — crystallize as the same agent that filed the proposals.
    # This is the #47 scenario: single-agent crystallize must succeed when
    # review.approver_role: trusted-agent is configured.
    import yaml
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg_path.write_text(yaml.safe_dump(cfg))
    cryst = handle_request({"id": "4", "method": "kb.crystallize",
                            "params": {"session_id": sid}})
    assert len(cryst["result"]["approved"]) == 1


def test_jsonl_self_approval_forbidden(store: KBStore, monkeypatch) -> None:
    """approve() must raise forbidden_self_approval when proposer == approver."""
    src = store.put_source(b"evidence")
    monkeypatch.chdir(store.root)
    pr = handle_request({"id": "1", "method": "kb.propose_claim",
                         "params": {"text": "test claim", "evidence": [src.id]}})
    pid = pr["result"]["proposal_id"]
    # Same agent approves — must fail
    resp = handle_request({"id": "2", "method": "kb.approve",
                           "params": {"proposal_id": pid}})
    assert not resp["ok"]
    assert "forbidden_self_approval" in resp["error"]["message"]


def test_jsonl_self_approval_allowed_with_trusted_agent_config(
    store: KBStore, monkeypatch
) -> None:
    """approve() must allow self-approval when review.approver_role=trusted-agent."""
    import yaml
    # Set trusted-agent opt-out in config
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg_path.write_text(yaml.safe_dump(cfg))

    src = store.put_source(b"evidence")
    monkeypatch.chdir(store.root)
    pr = handle_request({"id": "1", "method": "kb.propose_claim",
                         "params": {"text": "test claim", "evidence": [src.id]}})
    pid = pr["result"]["proposal_id"]
    resp = handle_request({"id": "2", "method": "kb.approve",
                           "params": {"proposal_id": pid}})
    assert resp["ok"]


def test_jsonl_internal_error_omits_traceback(monkeypatch) -> None:
    """HTTP /rpc clients must not receive server tracebacks on unexpected errors."""
    from vouch import jsonl_server

    def _boom(_params: dict) -> dict:
        raise RuntimeError("secret internals")

    monkeypatch.setitem(jsonl_server.HANDLERS, "kb.status", _boom)
    resp = handle_request({"id": "x", "method": "kb.status", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "internal_error"
    assert "traceback" not in resp["error"]


# --- gate integrity: the actor binds to the authenticated principal --------


def test_jsonl_agent_binds_to_auth_subject_over_header() -> None:
    """An authenticated request's actor is the token subject, not the header.

    X-Vouch-Agent is client-supplied; it must never override an authenticated
    identity, or one token could masquerade as any actor it likes.
    """
    from vouch import jsonl_server
    from vouch import trust as trust_mod

    trust = trust_mod.with_auth_subject(trust_mod.JSONL_HTTP, "one-token")
    with trust_mod.trust_context(trust):
        reset = jsonl_server._actor.set("spoofed-header")
        try:
            actor = jsonl_server._agent()
        finally:
            jsonl_server._actor.reset(reset)
    assert "spoofed-header" not in actor
    assert trust_mod.auth_subject_for_token("one-token") in actor


def test_jsonl_agent_uses_header_when_unauthenticated() -> None:
    """Tokenless (dev/loopback) requests keep the existing header attribution."""
    from vouch import jsonl_server
    from vouch import trust as trust_mod

    with trust_mod.trust_context(trust_mod.JSONL_HTTP):  # no auth_subject
        reset = jsonl_server._actor.set("named-agent")
        try:
            actor = jsonl_server._agent()
        finally:
            jsonl_server._actor.reset(reset)
    assert actor == "named-agent"


def test_one_token_cannot_self_approve_by_swapping_header(
    store: KBStore, monkeypatch
) -> None:
    """The core exploit is closed: propose-as-alice, approve-as-bob, one token.

    Because the actor binds to the authenticated subject, both operations
    resolve to the same actor and the self-approval gate blocks the approval.
    """
    from vouch import jsonl_server
    from vouch import trust as trust_mod

    src = store.put_source(b"evidence")
    monkeypatch.chdir(store.root)
    trust = trust_mod.with_auth_subject(trust_mod.JSONL_HTTP, "one-token")
    with trust_mod.trust_context(trust):
        reset = jsonl_server._actor.set("alice")
        try:
            pr = handle_request({"id": "1", "method": "kb.propose_claim",
                                 "params": {"text": "c", "evidence": [src.id]}})
        finally:
            jsonl_server._actor.reset(reset)
        pid = pr["result"]["proposal_id"]
        reset = jsonl_server._actor.set("bob")
        try:
            resp = handle_request({"id": "2", "method": "kb.approve",
                                   "params": {"proposal_id": pid}})
        finally:
            jsonl_server._actor.reset(reset)
    assert not resp["ok"]
    assert "forbidden_self_approval" in resp["error"]["message"]


def test_export_out_path_fenced_to_root_on_remote(store: KBStore, monkeypatch) -> None:
    """A remote caller cannot make kb.export clobber a file outside the root."""
    from vouch import trust as trust_mod

    monkeypatch.chdir(store.root)
    with trust_mod.trust_context(trust_mod.JSONL_HTTP):
        resp = handle_request({"id": "1", "method": "kb.export",
                               "params": {"out_path": "../escaped.tar.gz"}})
    assert not resp["ok"]
    assert "project root" in resp["error"]["message"]
    assert not (store.root.parent / "escaped.tar.gz").exists()


def test_export_within_root_still_works_on_remote(store: KBStore, monkeypatch) -> None:
    from vouch import trust as trust_mod

    monkeypatch.chdir(store.root)
    with trust_mod.trust_context(trust_mod.JSONL_HTTP):
        resp = handle_request({"id": "1", "method": "kb.export",
                               "params": {"out_path": "bundle.tar.gz"}})
    assert resp["ok"]
    assert (store.root / "bundle.tar.gz").exists()


def test_import_check_bundle_path_fenced_on_remote(store: KBStore, monkeypatch) -> None:
    """A remote caller cannot point kb.import_check at an arbitrary file."""
    from vouch import trust as trust_mod

    monkeypatch.chdir(store.root)
    with trust_mod.trust_context(trust_mod.JSONL_HTTP):
        resp = handle_request({"id": "1", "method": "kb.import_check",
                               "params": {"bundle_path": "/etc/passwd"}})
    assert not resp["ok"]
    assert "project root" in resp["error"]["message"]
