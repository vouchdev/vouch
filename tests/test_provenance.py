"""Tests for the provenance DAG: build, why/trace/impact, graph export, the
prov_edges cache, the CLI surface and the kb.* RPC methods.

The KB built by `_seed` exercises every edge kind the issue calls out: a claim
proposed in a session, citing a source, superseding an older claim, approved via
the audit log, and embedded by two live pages plus one draft.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from vouch import audit, index_db
from vouch import lifecycle as life
from vouch import provenance as prov
from vouch import sessions as sess_mod
from vouch.capabilities import capabilities
from vouch.cli import cli
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import (
    Claim,
    Evidence,
    Page,
    PageStatus,
    PageType,
    Proposal,
    ProposalKind,
)
from vouch.proposals import (
    approve,
    propose_claim,
    propose_delete,
    propose_entity,
    propose_page,
)
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


def _edge(graph, src: str, dst: str) -> str:
    """The kind of the single edge src -> dst, for readable assertions."""
    kinds = [e.kind.value for e in graph.out_edges(src) if e.dst_id == dst]
    assert len(kinds) == 1, f"expected one {src} -> {dst} edge, got {kinds}"
    return kinds[0]


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


# --- the pending frontier -------------------------------------------------


def test_pending_claim_is_a_node_keyed_on_the_proposal(store: KBStore) -> None:
    ids = _seed(store)
    pr = propose_claim(
        store, text="an unreviewed fact", evidence=[ids["src"]],
        proposed_by="agentA", slug_hint="c-pending", session_id=ids["session"],
    ).proposal

    graph = prov.build_graph(store)
    assert graph.kind_of(pr.id) is prov.NodeKind.PROPOSAL
    assert graph.status_of(pr.id) == "pending"
    assert graph.label_of(pr.id) == "an unreviewed fact"
    # The prospective claim id is not a node — until approval it does not exist.
    assert "c-pending" not in graph.nodes()
    assert _edge(graph, pr.id, ids["src"]) == "cites"
    assert _edge(graph, pr.id, ids["session"]) == "proposedIn"


def test_pending_delete_targets_the_artifact_it_would_remove(store: KBStore) -> None:
    ids = _seed(store)
    store.put_claim(Claim(id="c-lonely", text="referenced by nobody",
                          evidence=[ids["src"]]))
    pr = propose_delete(
        store, target_kind="claim", target_id="c-lonely", proposed_by="agentA",
    )

    graph = prov.build_graph(store)
    assert _edge(graph, pr.id, "c-lonely") == "targets"
    # and so the pending delete shows up as something depending on the claim
    dependents = prov.impact(store, claim_id="c-lonely")["dependents"]
    assert [d["source"] for d in dependents] == [pr.id]


def test_pending_page_embeds_the_claims_it_would_collect(store: KBStore) -> None:
    _seed(store)
    pr = propose_page(
        store, title="A pending page", body="draft", claim_ids=["c-new"],
        proposed_by="agentA",
    )

    graph = prov.build_graph(store)
    assert graph.label_of(pr.id) == "A pending page"
    assert _edge(graph, pr.id, "c-new") == "embeds"


def test_pending_proposal_with_no_edges_is_still_exported(store: KBStore) -> None:
    """An orphan proposal is the one a reviewer is most likely to forget."""
    _seed(store)
    pr = propose_entity(
        store, name="Acme Example", entity_type="company", proposed_by="agentA",
    )

    graph = prov.build_graph(store)
    assert pr.id not in graph.nodes()  # no edges to be found by
    exported = prov.graph_export(store, fmt="json")
    assert pr.id in [n["id"] for n in exported["nodes"]]
    # a session subgraph is edge-defined, so it does not pick the orphan up
    scoped = prov.graph_export(store, session="does-not-exist", fmt="json")
    assert scoped["nodes"] == []


def test_a_claim_citing_evidence_keeps_the_span_between_it_and_the_source(
    store: KBStore,
) -> None:
    """Citing an Evidence id, not a Source id, is the two-hop form."""
    src = store.put_source(b"the retry limit is 3", title="runbook")
    store.put_evidence(Evidence(id="ev-retry", source_id=src.id, locator="L1",
                                quote="the retry limit is 3"))
    store.put_claim(Claim(id="c-retry", text="retries stop at 3",
                          evidence=["ev-retry"]))

    graph = prov.build_graph(store)
    assert graph.kind_of("ev-retry") is prov.NodeKind.EVIDENCE
    assert graph.kind_of(src.id) is prov.NodeKind.SOURCE
    assert _edge(graph, "c-retry", "ev-retry") == "cites"
    assert _edge(graph, "ev-retry", src.id) == "derivedFrom"


def test_a_bare_edge_list_still_reads_a_delete_proposal_as_one() -> None:
    """A graph handed only edges — an older cache — infers kinds from them."""
    graph = prov.ProvGraph([prov.Edge("20260101-000000-abcd1234", "c-1",
                                      prov.EdgeKind.TARGETS)])
    assert graph.kind_of("20260101-000000-abcd1234") is prov.NodeKind.PROPOSAL
    assert graph.status_of("20260101-000000-abcd1234") == ""
    assert graph.label_of("c-1") == "c-1"


def test_a_proposal_the_payload_cannot_describe_falls_back_to_its_id(
    store: KBStore,
) -> None:
    """Payloads written by an older vouch still have to render."""
    store.put_proposal(
        Proposal(
            id="20260101-000000-deadbeef", kind=ProposalKind.CLAIM,
            proposed_by="agentA", payload={"id": "c-odd", "evidence": "not-a-list"},
        )
    )
    graph = prov.build_graph(store)
    assert graph.label_of("20260101-000000-deadbeef") == "20260101-000000-deadbeef"
    assert graph.out_edges("20260101-000000-deadbeef") == []


# --- the json format ------------------------------------------------------


def test_graph_export_json_carries_kind_status_and_label(store: KBStore) -> None:
    ids = _seed(store)
    pending = propose_claim(
        store, text="an unreviewed fact", evidence=[ids["src"]],
        proposed_by="agentA", slug_hint="c-pending",
    ).proposal

    data = prov.graph_export(store, fmt="json")
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["c-new"] == {
        "id": "c-new", "kind": "claim", "label": "the newer fact",
        "status": "working",
    }
    assert by_id["c-old"]["status"] == "superseded"
    assert by_id["page-alpha"] == {
        "id": "page-alpha", "kind": "page", "label": "Alpha", "status": "active",
    }
    assert by_id[pending.id]["status"] == "pending"
    # structural nodes have no review status of their own
    assert by_id[ids["src"]] == {
        "id": ids["src"], "kind": "source", "label": ids["src"], "status": "",
    }
    assert {"src": "page-alpha", "dst": "c-new", "kind": "embeds"} in data["edges"]


def test_graph_export_json_reads_no_artifact_per_node(store: KBStore) -> None:
    """The whole point of carrying status on the graph: json stays a formatter.

    `dot` and `mermaid` do zero I/O once the graph is loaded. If `json` had to
    re-fetch each node to learn its status it would be the expensive format on
    a path that exists to avoid re-reading.
    """
    _seed(store)
    prov.load_graph(store)  # warm the cache; the export below must not re-read

    def boom(*_args, **_kwargs):
        raise AssertionError("graph_export re-read an artifact from disk")

    with patch.object(KBStore, "get_claim", boom), patch.object(KBStore, "get_page", boom):
        data = prov.graph_export(store, fmt="json")
    assert data["nodes"] and data["edges"]


def test_cached_and_freshly_built_graphs_agree_on_status(store: KBStore) -> None:
    ids = _seed(store)
    propose_claim(
        store, text="an unreviewed fact", evidence=[ids["src"]],
        proposed_by="agentA", slug_hint="c-pending",
    )
    fresh = prov.graph_export(store, fmt="json", use_cache=False)
    cached = prov.graph_export(store, fmt="json", use_cache=True)
    assert cached == fresh


def test_filing_a_proposal_invalidates_the_cache(store: KBStore) -> None:
    ids = _seed(store)
    before = prov.prov_stamp(store)
    prov.load_graph(store)
    pr = propose_claim(
        store, text="an unreviewed fact", evidence=[ids["src"]],
        proposed_by="agentA", slug_hint="c-pending",
    ).proposal
    assert prov.prov_stamp(store) != before
    assert pr.id in prov.load_graph(store).meta()


def test_cached_node_of_an_unknown_kind_is_dropped(store: KBStore) -> None:
    """A cache written by a newer vouch must not break an older one."""
    _seed(store)
    prov.rebuild_prov_edges(store)
    with index_db.open_db(store.kb_dir) as conn:
        index_db.index_prov_node(conn, id="c-new", kind="hologram")
    assert "c-new" not in prov.cache.load_meta(store)


def test_graph_export_rejects_an_unknown_format(store: KBStore) -> None:
    _seed(store)
    with pytest.raises(ValueError, match="dot"):
        prov.graph_export(store, fmt="svg")


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


def test_cli_graph_json(store: KBStore) -> None:
    _seed(store)
    res = CliRunner().invoke(cli, ["graph", "--format", "json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert {n["id"] for n in data["nodes"]} >= {"c-new", "page-alpha"}


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


def test_kb_graph_export_json_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "5", "method": "kb.graph_export",
                           "params": {"format": "json"}})
    assert resp["ok"] is True, resp
    assert resp["result"]["format"] == "json"
    assert "c-new" in [n["id"] for n in resp["result"]["graph"]["nodes"]]


def test_kb_why_missing_param_over_jsonl(store: KBStore) -> None:
    _seed(store)
    resp = handle_request({"id": "4", "method": "kb.why", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"


# --- archived pages are out of the live set (#701) --------------------------


def test_archived_pages_contribute_no_nodes_or_edges(store: KBStore) -> None:
    """Regression for #701: `build_graph` walked every page with no lifecycle
    filter, so a retired topic stayed in the provenance graph — undoing
    archive for anything that renders it."""
    from vouch.provenance.graph import build_graph

    _seed(store)
    store.put_page(Page(id="page-dead", title="Dead", type=PageType.CONCEPT,
                        claims=["c-new"], status=PageStatus.ARCHIVED))

    graph = build_graph(store)
    assert not [e for e in graph.edges if e.src_id == "page-dead"]
    assert "page-dead" not in graph.nodes()


def test_live_and_draft_pages_still_embed(store: KBStore) -> None:
    """The filter is archive-only: a draft page is unreviewed, not retired,
    and the seed's two active pages must keep their EMBEDS edges."""
    from vouch.provenance.graph import build_graph

    _seed(store)
    embedders = {
        e.src_id for e in build_graph(store).edges if e.kind.value == "embeds"
    }
    assert {"page-alpha", "page-beta", "page-draft"} <= embedders


def test_archiving_a_page_removes_it_from_impact(store: KBStore) -> None:
    """The user-visible consequence: `impact` stops naming a page the wiki
    no longer carries."""
    _seed(store)
    before = prov.impact(store, claim_id="c-new", depth=2)
    assert "page-alpha" in str(before["dependents"])

    page = store.get_page("page-alpha")
    page.status = PageStatus.ARCHIVED
    store.update_page(page)

    after = prov.impact(store, claim_id="c-new", depth=2, use_cache=False)
    assert "page-alpha" not in str(after["dependents"])
    assert "page-beta" in str(after["dependents"])
