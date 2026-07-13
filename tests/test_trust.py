"""``_meta.vouch_trust`` on kb.* responses (#233)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from vouch import trust as trust_mod
from vouch.jsonl_server import handle_request
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"evidence")
    s.put_claim(Claim(id="c1", text="JWT rotation policy", evidence=[src.id]))
    return s


@pytest.fixture(autouse=True)
def _jsonl_trust_default() -> Iterator[None]:
    trust_mod.set_stdio_default(trust_mod.JSONL_STDIO)
    yield


# --- unit: trust module ----------------------------------------------------


def test_auth_subject_fingerprints_without_echoing_secret() -> None:
    fp = trust_mod.auth_subject_for_token("super-secret-token")
    assert fp != "super-secret-token"
    assert len(fp) == 16
    assert trust_mod.auth_subject_for_token("super-secret-token") == fp


def test_matched_bearer_token_constant_time_membership() -> None:
    assert trust_mod.matched_bearer_token("Bearer alpha", ("alpha", "beta")) == "alpha"
    assert trust_mod.matched_bearer_token("Bearer beta", ("alpha", "beta")) == "beta"
    assert trust_mod.matched_bearer_token("Bearer gamma", ("alpha", "beta")) is None
    assert trust_mod.matched_bearer_token(None, ("alpha",)) is None


def test_attach_trust_preserves_existing_meta() -> None:
    with trust_mod.trust_context(trust_mod.CLI):
        out = trust_mod.attach_trust({"_meta": {"vouch_salience": []}, "hits": []})
    assert out["_meta"]["vouch_salience"] == []
    assert out["_meta"]["vouch_trust"]["caller_kind"] == "cli"
    assert out["_meta"]["vouch_trust"]["remote"] is False


def test_finish_kb_result_skips_non_dicts() -> None:
    with trust_mod.trust_context(trust_mod.JSONL_STDIO):
        assert trust_mod.finish_kb_result([{"id": "x"}]) == [{"id": "x"}]


# --- JSONL: every dict-shaped read carries trust -------------------------


READ_DICT_CASES: tuple[tuple[str, dict], ...] = (
    ("kb.capabilities", {}),
    ("kb.status", {}),
    ("kb.stats", {}),
    ("kb.search", {"query": "JWT", "limit": 3}),
    ("kb.context", {"task": "JWT", "limit": 3}),
    ("kb.lint", {}),
    ("kb.doctor", {}),
    ("kb.graph_export", {}),
)


@pytest.mark.parametrize(("method", "params"), READ_DICT_CASES)
def test_jsonl_read_responses_carry_vouch_trust(
    store: KBStore,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict,
) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "t", "method": method, "params": params})
    assert resp["ok"], resp
    trust_mod.assert_vouch_trust(resp["result"])
    assert resp["result"]["_meta"]["vouch_trust"]["caller_kind"] == "jsonl"
    assert resp["result"]["_meta"]["vouch_trust"]["remote"] is False


def test_jsonl_read_claim_carries_trust(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({
        "id": "t",
        "method": "kb.read_claim",
        "params": {"claim_id": "c1"},
    })
    assert resp["ok"]
    trust_mod.assert_vouch_trust(resp["result"])


def test_jsonl_http_trust_preset() -> None:
    with trust_mod.trust_context(trust_mod.JSONL_HTTP):
        block = trust_mod.attach_trust({})["_meta"]["vouch_trust"]
    assert block == {
        "remote": True,
        "caller_kind": "jsonl_http",
        "auth_subject": None,
    }


def test_jsonl_http_bearer_auth_subject() -> None:
    trust = trust_mod.with_auth_subject(trust_mod.JSONL_HTTP, "fleet-alpha")
    with trust_mod.trust_context(trust):
        block = trust_mod.attach_trust({})["_meta"]["vouch_trust"]
    assert block["auth_subject"] == trust_mod.auth_subject_for_token("fleet-alpha")


def test_mcp_http_trust_on_tool_result(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance: HTTP-MCP surfaces remote mcp_http (via tool wrapper + context)."""
    from vouch.server import mcp

    monkeypatch.chdir(store.root)
    tool = mcp._tool_manager.get_tool("kb_status")
    assert tool is not None
    with trust_mod.trust_context(trust_mod.MCP_HTTP):
        result = tool.fn()
    trust_mod.assert_vouch_trust(result)
    assert result["_meta"]["vouch_trust"] == {
        "remote": True,
        "caller_kind": "mcp_http",
        "auth_subject": None,
    }


def test_mcp_stdio_trust_on_tool_result(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from vouch.server import mcp

    monkeypatch.chdir(store.root)
    tool = mcp._tool_manager.get_tool("kb_search")
    assert tool is not None
    with trust_mod.trust_context(trust_mod.MCP_STDIO):
        result = tool.fn("JWT", limit=3)
    assert result["_meta"]["vouch_trust"]["caller_kind"] == "mcp_stdio"
    assert result["_meta"]["vouch_trust"]["remote"] is False


# --- CLI -------------------------------------------------------------------


def test_cli_status_json_surfaces_cli_trust(
    store: KBStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    proc = subprocess.run(
        [sys.executable, "-m", "vouch.cli", "status", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    trust_mod.assert_vouch_trust(body)
    assert body["_meta"]["vouch_trust"] == {
        "remote": False,
        "caller_kind": "cli",
        "auth_subject": None,
    }


def test_read_methods_constant_covers_declared_reads() -> None:
    """READ_METHODS stays aligned with capabilities — drift means a test gap."""
    from vouch.capabilities import METHODS

    read_prefixes = ("kb.read_", "kb.list_", "kb.search", "kb.context", "kb.status")
    declared_reads = {
        m for m in METHODS
        if m.startswith(read_prefixes) or m in {
            "kb.capabilities",
            "kb.stats",
            "kb.activity",
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
        }
    }
    assert set(trust_mod.READ_METHODS) <= declared_reads
