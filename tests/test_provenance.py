"""Tests for the provenance DAG: build, why/trace/impact, graph export, the
prov_edges cache, the CLI surface and the kb.* RPC methods.

The KB built by `_seed` exercises every edge kind the issue calls out: a claim
proposed in a session, citing a source, superseding an older claim, approved via
the audit log, and embedded by two live pages plus one draft.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from vouch import audit
from vouch import lifecycle as life
from vouch import provenance as prov
from vouch import sessions as sess_mod
from vouch.capabilities import capabilities
from vouch.cli import cli
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import Claim, Page, PageStatus, PageType
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path, monkeypatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _approve_event_id(store: KBStore, claim_id: str) -> str:
    for ev in audit.read_events(store.kb_dir):
        if ev.event.endswith(".approve") and claim_id in ev.object_ids:
            return ev.id
    raise AssertionError(f"no approve event for {claim_id}")


def _seed(store: KBStore) -> dict[str, str]:
    """A small but complete KB. Returns the ids the tests assert against."""
    src = store.put_source(b"a primary source", title="Source S")
    sess = sess_mod.session_start(store, agent="agentA", task="seed the KB")

    pr_old = propose_claim(
        store, text="the older fact", evidence=[src.id], proposed_by="agentA",
        slug_hint="c-old", session_id=sess.id,
    )
    approve(store, pr_old.proposal.id, approved_by="reviewer")

    pr_new = propose_claim(
        store, text="the newer fact", evidence=[src.id], proposed_by="agentA",
        slug_hint="c-new", session_id=sess.id,
    )
    approve(store, pr_new.proposal.id, approved_by="reviewer")
    sess_mod.session_end(store, sess.id)

    # c-new supersedes c-old (writes the supersedes relation + claim fields)
    life.supersede(store, old_claim_id="c-old", new_claim_id="c-new", actor="reviewer")

    # two *active* pages embed c-new (breakage on archive) + one draft (ignored)
    store.put_page(Page(id="page-alpha", title="Alpha", type=PageType.CONCEPT,
                        claims=["c-new"], status=PageStatus.ACTIVE))
    store.put_page(Page(id="page-beta", title="Beta", type=PageType.CONCEPT,
                        claims=["c-new"], status=PageStatus.ACTIVE))
    store.put_page(Page(id="page-draft", title="Draft", type=PageType.CONCEPT,
                        claims=["c-new"], status=PageStatus.DRAFT))

    return {
        "src": src.id,
        "session": sess.id,
        "approve_event": _approve_event_id(store, "c-new"),
    }


# --- acceptance 1: why ----------------------------------------------------


def test_why_has_session_source_and_supersedes_branches(store: KBStore) -> None:
    ids = _seed(store)
    result = prov.why(store, claim_id="c-new", depth=3)

    assert result["root"] == "c-new"
    assert result["node_kind"] == "claim"
    by_kind = {e["kind"]: e for e in result["provenance"]}
    # all three branches the acceptance names, plus the approval event
    assert by_kind["cites"]["target"] == ids["src"]
    assert by_kind["proposedIn"]["target"] == ids["session"]
    assert by_kind["supersedes"]["target"] == "c-old"
    assert by_kind["approvedBy"]["target"] == ids["approve_event"]
    # the approval branch carries the originating audit timestamp
    assert by_kind["approvedBy"]["event_ts"]


def test_why_unknown_claim_raises(store: KBStore) -> None:
    _seed(store)
    from vouch.storage import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError):
        prov.why(store, claim_id="nope")


# --- acceptance 2: impact + --if archive ----------------------------------


def test_impact_if_archive_lists_active_pages_and_blocks(store: KBStore) -> None:
    _seed(store)
    result = prov.impact(store, claim_id="c-new", op="archive")

    broken_ids = sorted(b["id"] for b in result["breakage"])
    assert broken_ids == ["page-alpha", "page-beta"]  # draft excluded
    assert result["blocking"] is True
    assert result["op"] == "archive"


def test_impact_without_op_lists_but_does_not_block(store: KBStore) -> None:
    _seed(store)
    result = prov.impact(store, claim_id="c-new")

    assert result["op"] is None
    assert result["blocking"] is False
    assert result["breakage"] == []
    # the embedding pages still show up as dependents
    dep_sources = {d["source"] for d in result["dependents"]}
    assert {"page-alpha", "page-beta", "page-draft"} <= dep_sources


def test_impact_reverse_supersedes_label(store: KBStore) -> None:
    _seed(store)
    result = prov.impact(store, claim_id="c-old")
    labels = {(d["kind"], d["source"]) for d in result["dependents"]}
    assert ("supersededBy", "c-new") in labels


# --- acceptance 3: trace --------------------------------------------------


def test_trace_finds_typed_path(store: KBStore) -> None:
    ids = _seed(store)
    result = prov.trace(store, from_id="page-alpha", to_id="c-old")
    assert result["found"] is True
    # page-alpha --embeds--> c-new --supersedes--> c-old
    assert result["nodes"] == ["page-alpha", "c-new", "c-old"]
    kinds = [s["kind"] for s in result["path"]]
    assert kinds == ["embeds", "supersedes"]

    direct = prov.trace(store, from_id="c-new", to_id=ids["src"])
    assert direct["found"] is True and direct["length"] == 1


def test_trace_no_path_is_not_found(store: KBStore) -> None:
    _seed(store)
    result = prov.trace(store, from_id="c-new", to_id="not-an-artifact")
    assert result["found"] is False
    assert result["path"] == []


# --- acceptance 4: rebuild byte-equivalence -------------------------------


def _edge_tuples(edges) -> list[tuple]:
    return sorted(
        (e.src_id, e.dst_id, e.kind.value, e.event_ts, e.session_id) for e in edges
    )


def test_rebuild_matches_live_graph(store: KBStore) -> None:
    _seed(store)
    live = prov.build_graph(store).edges
    n = prov.rebuild_prov_edges(store)
    cached = prov.cache.load_edges(store)
    assert n == len(live)
    assert _edge_tuples(cached) == _edge_tuples(live)


def test_rebuild_is_idempotent(store: KBStore) -> None:
    _seed(store)
    first = prov.rebuild_prov_edges(store)
    rows1 = _edge_tuples(prov.cache.load_edges(store))
    second = prov.rebuild_prov_edges(store)
    rows2 = _edge_tuples(prov.cache.load_edges(store))
    assert first == second
    assert rows1 == rows2


def test_load_graph_uses_cache_then_refreshes_on_change(store: KBStore) -> None:
    _seed(store)
    g1 = prov.load_graph(store)  # builds + caches
    assert g1.edges
    # add a new claim -> stamp changes -> load_graph must rebuild and include it
    src2 = store.put_source(b"second source")
    store.put_claim(Claim(id="c-extra", text="extra", evidence=[src2.id]))
    g2 = prov.load_graph(store)
    assert "c-extra" in g2.nodes()


# --- graph export ---------------------------------------------------------


def test_graph_export_dot_and_mermaid(store: KBStore) -> None:
    _seed(store)
    dot = prov.graph_export(store, fmt="dot")
    assert dot.startswith("digraph provenance")
    assert "embeds" in dot and "c-new" in dot

    mer = prov.graph_export(store, fmt="mermaid")
    assert mer.startswith("flowchart LR")

    with pytest.raises(ValueError):
        prov.graph_export(store, fmt="svg")


def test_graph_export_session_subgraph(store: KBStore) -> None:
    ids = _seed(store)
    dot = prov.graph_export(store, session=ids["session"], fmt="dot")
    assert "c-new" in dot and "c-old" in dot


# --- CLI ------------------------------------------------------------------


def test_cli_why_json(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["why", "c-new", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["root"] == "c-new"
    assert any(e["kind"] == "supersedes" for e in data["provenance"])


def test_cli_impact_if_archive_exits_nonzero(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["impact", "c-new", "--if", "archive"])
    assert res.exit_code == 1, res.output
    assert "page-alpha" in res.output and "page-beta" in res.output


def test_cli_impact_without_op_exits_zero(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["impact", "c-new"])
    assert res.exit_code == 0, res.output


def test_cli_trace_no_path_exits_nonzero(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["trace", "c-new", "--to", "ghost"])
    assert res.exit_code == 1, res.output
    assert "no path" in res.output


def test_cli_trace_found(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["trace", "page-alpha", "--to", "c-old", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["found"] is True


def test_cli_provenance_rebuild(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["provenance", "rebuild", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["edges"] > 0


def test_cli_graph_dot(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["graph", "--format", "dot"])
    assert res.exit_code == 0, res.output
    assert res.output.startswith("digraph provenance")


# --- kb.* RPC surface -----------------------------------------------------


def test_provenance_methods_in_capabilities() -> None:
    methods = set(capabilities().methods)
    assert {"kb.why", "kb.trace", "kb.impact", "kb.graph_export",
            "kb.provenance_rebuild"} <= methods
    # contract: every advertised method has a JSONL handler
    assert set(capabilities().methods) == set(HANDLERS.keys())


def test_kb_why_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "1", "method": "kb.why",
                           "params": {"claim_id": "c-new"}})
    assert resp["ok"] is True, resp
    assert resp["result"]["root"] == "c-new"


def test_kb_impact_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "2", "method": "kb.impact",
                           "params": {"claim_id": "c-new", "op": "archive"}})
    assert resp["ok"] is True, resp
    assert resp["result"]["blocking"] is True


def test_kb_trace_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "3", "method": "kb.trace",
                           "params": {"from": "page-alpha", "to": "c-old"}})
    assert resp["ok"] is True, resp
    assert resp["result"]["found"] is True


def test_kb_why_missing_param_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "4", "method": "kb.why", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"
