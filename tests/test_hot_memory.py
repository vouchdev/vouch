"""Hot-memory sidebar — gbrain's ``_meta.brain_hot_memory`` pattern."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vouch import hot_memory as hot_mod
from vouch.capabilities import METHODS
from vouch.jsonl_server import handle_request
from vouch.models import ClaimStatus, Entity, EntityType, Page, PageType
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _clear_cache():
    hot_mod.reset_sidebar_cache()
    yield
    hot_mod.reset_sidebar_cache()


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _approved_claim(
    store: KBStore, text: str, *, approver: str = "reviewer",
) -> str:
    src = store.put_source(b"evidence-bytes")
    pr = propose_claim(store, text=text, evidence=[src.id], proposed_by="agent-A")
    artifact = approve(store, pr.id, approved_by=approver)
    return artifact.id  # type: ignore[union-attr]


# --- core sidebar shape -----------------------------------------------------


def test_recently_approved_claim_appears(store: KBStore) -> None:
    _approved_claim(store, "the sky is blue today")
    rows = hot_mod.compute_hot_memory(store, limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "the sky is blue today"
    assert row["status"] == ClaimStatus.WORKING.value
    assert row["why_hot"] == "recent"
    assert "approved_at" in row


def test_empty_kb_yields_empty_list(store: KBStore) -> None:
    assert hot_mod.compute_hot_memory(store, limit=5) == []


def test_limit_caps_result_size(store: KBStore) -> None:
    for i in range(5):
        _approved_claim(store, f"claim number {i}")
    rows = hot_mod.compute_hot_memory(store, limit=2)
    assert len(rows) == 2


# --- query bias -------------------------------------------------------------


def test_query_bias_boosts_matching_claim(store: KBStore) -> None:
    id_off = _approved_claim(store, "the moon orbits earth")
    id_on = _approved_claim(store, "kafka partitioning explained")
    rows = hot_mod.compute_hot_memory(store, query="kafka", limit=5)
    assert rows[0]["id"] == id_on
    assert rows[0]["why_hot"] == "recent+match"
    assert any(r["id"] == id_off for r in rows)


def test_query_bias_for_entity_name_and_aliases(store: KBStore) -> None:
    _approved_claim(store, "acme corp uses kafka for events")
    _approved_claim(store, "unrelated weather report")
    entity = Entity(id="ent-acme", name="Acme Corp", type=EntityType.COMPANY,
                    aliases=["ACME", "Acme"])
    store.put_entity(entity)
    bias = hot_mod.query_bias_for_entity(entity)
    rows = hot_mod.compute_hot_memory(store, query=bias, limit=5)
    assert rows[0]["why_hot"] == "recent+match"


def test_query_bias_for_page_title_and_tags(store: KBStore) -> None:
    _approved_claim(store, "security review checklist for deploys")
    _approved_claim(store, "unrelated lunch menu")
    page = Page(id="pg-sec", title="Security Review", type=PageType.DECISION.value,
                tags=["security", "deploy"])
    store.put_page(page)
    bias = hot_mod.query_bias_for_page(page)
    rows = hot_mod.compute_hot_memory(store, query=bias, limit=5)
    assert any(r["why_hot"] == "recent+match" for r in rows)


# --- filtering: status + age ----------------------------------------------


def test_archived_claim_excluded(store: KBStore) -> None:
    cid = _approved_claim(store, "to be archived")
    claim = store.get_claim(cid)
    claim.status = ClaimStatus.ARCHIVED
    store.update_claim(claim)
    assert hot_mod.compute_hot_memory(store, limit=5) == []


def test_superseded_claim_excluded(store: KBStore) -> None:
    cid = _approved_claim(store, "to be superseded")
    claim = store.get_claim(cid)
    claim.status = ClaimStatus.SUPERSEDED
    store.update_claim(claim)
    assert hot_mod.compute_hot_memory(store, limit=5) == []


def test_old_claim_filtered_by_max_age(store: KBStore) -> None:
    cid = _approved_claim(store, "ancient history")
    claim = store.get_claim(cid)
    claim.updated_at = datetime.now(UTC) - timedelta(days=30)
    store.update_claim(claim)
    rows = hot_mod.compute_hot_memory(store, limit=5, max_age_seconds=24 * 3600)
    assert rows == []


# --- exclude_ids + cache --------------------------------------------------


def test_exclude_ids_filters_caller_supplied(store: KBStore) -> None:
    a = _approved_claim(store, "alpha")
    b = _approved_claim(store, "beta")
    rows = hot_mod.compute_hot_memory(store, limit=5, exclude_ids=[a])
    ids = [r["id"] for r in rows]
    assert a not in ids
    assert b in ids


def test_cache_returns_stale_within_ttl(store: KBStore) -> None:
    _approved_claim(store, "first claim")
    rows1 = hot_mod.compute_hot_memory(store, limit=5, now=1000.0)
    _approved_claim(store, "second claim")
    rows2 = hot_mod.compute_hot_memory(store, limit=5, now=1000.5)
    assert len(rows1) == 1
    assert len(rows2) == 1


def test_cache_expires_after_ttl(store: KBStore) -> None:
    _approved_claim(store, "first claim")
    hot_mod.compute_hot_memory(store, limit=5, now=1000.0)
    _approved_claim(store, "second claim")
    rows = hot_mod.compute_hot_memory(store, limit=5, now=1100.0)
    assert len(rows) == 2


# --- attach_hot_memory shape contracts --------------------------------------


def test_attach_to_dict_merges_meta(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    result: dict = {"foo": "bar"}
    wrapped = hot_mod.attach_hot_memory(result, store, query=None)
    assert wrapped is result
    assert "vouch_hot_memory" in wrapped["_meta"]
    assert wrapped["foo"] == "bar"


def test_attach_preserves_existing_meta(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    result: dict = {"foo": "bar", "_meta": {"caller_meta": "keep me"}}
    wrapped = hot_mod.attach_hot_memory(result, store, query=None)
    assert wrapped["_meta"]["caller_meta"] == "keep me"
    assert "vouch_hot_memory" in wrapped["_meta"]


def test_attach_to_list_wraps_in_envelope(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    result_list = [{"hit": 1}, {"hit": 2}]
    wrapped = hot_mod.attach_hot_memory(result_list, store, query=None)
    assert isinstance(wrapped, dict)
    assert wrapped["items"] == result_list
    assert "vouch_hot_memory" in wrapped["_meta"]


def test_list_envelope_always_wraps_with_deprecation(store: KBStore) -> None:
    result_list = [{"id": "x"}]
    wrapped = hot_mod.attach_hot_memory(
        result_list, store, query=None, list_envelope=True,
    )
    assert wrapped["items"] == result_list
    assert "deprecation" in wrapped["_meta"]
    assert wrapped["_meta"]["deprecation"]["migration"]


def test_list_envelope_includes_sidebar_when_non_empty(store: KBStore) -> None:
    _approved_claim(store, "fresh claim text")
    wrapped = hot_mod.attach_hot_memory(
        [], store, query=None, list_envelope=True,
    )
    assert wrapped["items"] == []
    assert "vouch_hot_memory" in wrapped["_meta"]
    assert "deprecation" in wrapped["_meta"]


def test_attach_empty_sidebar_is_noop(store: KBStore) -> None:
    result: dict = {"foo": "bar"}
    out = hot_mod.attach_hot_memory(result, store, query=None)
    assert out is result
    assert "_meta" not in out


def test_attach_scalar_unchanged(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    assert hot_mod.attach_hot_memory("string", store, query=None) == "string"


def test_compute_returns_empty_on_broken_store(tmp_path: Path) -> None:
    class BrokenStore:
        def __init__(self) -> None:
            self.kb_dir = tmp_path / ".vouch"

        def list_claims(self) -> list:
            raise RuntimeError("simulated read failure")

    rows = hot_mod.compute_hot_memory(BrokenStore(), limit=5)  # type: ignore[arg-type]
    assert rows == []


# --- JSONL integration ------------------------------------------------------


def test_jsonl_read_claim_attaches_hot_memory(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid_target = _approved_claim(store, "target claim about jwt rotation")
    _approved_claim(store, "adjacent claim about jwt expiry")

    monkeypatch.chdir(store.root)
    result = handle_request(
        {"id": "r1", "method": "kb.read_claim", "params": {"claim_id": cid_target}},
    )
    assert result["ok"] is True
    payload = result["result"]
    assert payload["id"] == cid_target
    hot = payload["_meta"]["vouch_hot_memory"]
    assert all(r["id"] != cid_target for r in hot)
    assert any("jwt expiry" in r["text"] for r in hot)


def test_jsonl_list_claims_uses_envelope(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _approved_claim(store, "listed claim")
    monkeypatch.chdir(store.root)
    result = handle_request({"id": "r1", "method": "kb.list_claims", "params": {}})
    assert result["ok"]
    payload = result["result"]
    assert "items" in payload
    assert "deprecation" in payload["_meta"]
    assert len(payload["items"]) == 1


def test_jsonl_list_pending_recency_only_no_query_bias(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = store.put_source(b"e")
    propose_claim(store, text="pending jwt topic", evidence=[src.id],
                  proposed_by="agent")
    _approved_claim(store, "approved unrelated")
    monkeypatch.chdir(store.root)
    result = handle_request({"id": "r1", "method": "kb.list_pending", "params": {}})
    assert result["ok"]
    assert len(result["result"]["items"]) == 1
    hot = result["result"]["_meta"].get("vouch_hot_memory", [])
    assert len(hot) >= 1
    assert all(r["why_hot"] == "recent" for r in hot)


# --- universal coverage (#225 acceptance) -----------------------------------


def test_hot_memory_universal_coverage() -> None:
    """Every kb.* method either attaches hot memory or is explicitly excluded."""
    covered = hot_mod.HOT_MEMORY_COVERED
    excluded = set(hot_mod.HOT_MEMORY_EXCLUDED)
    declared = set(METHODS)

    assert covered <= declared
    assert excluded <= declared
    assert not covered & excluded

    uncovered = declared - covered - excluded
    assert not uncovered, (
        f"methods missing from HOT_MEMORY_COVERED or HOT_MEMORY_EXCLUDED: "
        f"{sorted(uncovered)}"
    )


@pytest.mark.parametrize("method", sorted(hot_mod.HOT_MEMORY_COVERED))
def test_covered_methods_attach_sidebar_when_kb_has_recent_claims(
    store: KBStore,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Smoke: each covered method returns vouch_hot_memory on a warm KB."""
    cid = _approved_claim(store, "kafka stream processing policy")
    _approved_claim(store, "adjacent kafka consumer group notes")
    monkeypatch.chdir(store.root)

    params: dict = {}
    if method == "kb.search":
        params = {"query": "xyzzy_no_index_hits", "limit": 3}
    elif method == "kb.context":
        params = {"task": "xyzzy_no_index_hits", "limit": 3}
    elif method == "kb.read_page":
        page = Page(id="pg-k", title="Kafka Guide", type=PageType.CONCEPT.value,
                    tags=["kafka"])
        store.put_page(page)
        params = {"page_id": "pg-k"}
    elif method == "kb.read_claim":
        params = {"claim_id": cid}
    elif method == "kb.read_entity":
        ent = Entity(id="ent-k", name="Kafka", type=EntityType.CONCEPT,
                     aliases=["Apache Kafka"])
        store.put_entity(ent)
        params = {"entity_id": "ent-k"}
    elif method == "kb.read_relation":
        ent = Entity(id="ent-a", name="A", type=EntityType.CONCEPT)
        ent2 = Entity(id="ent-b", name="B", type=EntityType.CONCEPT)
        store.put_entity(ent)
        store.put_entity(ent2)
        from vouch.models import Relation, RelationType
        rel = Relation(id="rel-ab", source="ent-a", relation=RelationType.RELATES_TO,
                       target="ent-b")
        store.put_relation(rel)
        params = {"relation_id": "rel-ab"}

    resp = handle_request({"id": "cov", "method": method, "params": params})
    assert resp["ok"], resp
    payload = resp["result"]
    if method.startswith("kb.list_"):
        assert "items" in payload
        meta = payload["_meta"]
    else:
        meta = payload["_meta"]
    assert "vouch_hot_memory" in meta
    assert len(meta["vouch_hot_memory"]) >= 1
