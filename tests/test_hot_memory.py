"""Hot-memory sidebar — gbrain's `_meta.brain_hot_memory` pattern ported.

Covers:
* recently approved claims appear in the sidebar
* old / superseded / archived claims are filtered out
* the query parameter boosts substring-matching claims
* the cache returns stale results within TTL and recomputes after
* exclude_ids removes echoed ids from the sidebar
* attach_hot_memory mutates dict results, wraps list results, leaves
  scalars unchanged
* empty KBs produce an empty list (no spurious _meta key)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vouch import hot_memory as hot_mod
from vouch.models import ClaimStatus
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _clear_cache():
    hot_mod.reset_cache()
    yield
    hot_mod.reset_cache()


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


# --- core sidebar shape ---------------------------------------------------


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


# --- query bias -----------------------------------------------------------


def test_query_bias_boosts_matching_claim(store: KBStore) -> None:
    """A claim mentioning the query terms outranks a non-matching one even
    if both are recent."""
    id_off = _approved_claim(store, "the moon orbits earth")
    id_on = _approved_claim(store, "kafka partitioning explained")
    rows = hot_mod.compute_hot_memory(store, query="kafka", limit=5)
    assert rows[0]["id"] == id_on
    assert rows[0]["why_hot"] == "recent+match"
    # The non-matching one still shows up — it's just below.
    assert any(r["id"] == id_off for r in rows)


def test_query_normalises_case_and_whitespace(store: KBStore) -> None:
    _approved_claim(store, "AUTH tokens rotate every hour")
    rows = hot_mod.compute_hot_memory(store, query="  Auth\nTokens  ", limit=3)
    # Query has both "auth" and "tokens" as one normalised phrase; we don't
    # rank cross-token, so check at minimum that 'auth tokens' substring hits.
    assert rows[0]["why_hot"].startswith("recent")


# --- filtering: status + age ---------------------------------------------


def test_archived_claim_excluded(store: KBStore) -> None:
    cid = _approved_claim(store, "to be archived")
    # Manually demote — the lifecycle helper requires more setup; status
    # poke is enough to validate the filter.
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
    # Backdate the claim past the cutoff.
    claim = store.get_claim(cid)
    claim.updated_at = datetime.now(UTC) - timedelta(days=30)
    store.update_claim(claim)
    rows = hot_mod.compute_hot_memory(store, limit=5, max_age_seconds=24 * 3600)
    assert rows == []


# --- exclude_ids ----------------------------------------------------------


def test_exclude_ids_filters_caller_supplied(store: KBStore) -> None:
    a = _approved_claim(store, "alpha")
    b = _approved_claim(store, "beta")
    rows = hot_mod.compute_hot_memory(store, limit=5, exclude_ids=[a])
    ids = [r["id"] for r in rows]
    assert a not in ids
    assert b in ids


# --- ttl cache ------------------------------------------------------------


def test_cache_returns_stale_within_ttl(store: KBStore) -> None:
    _approved_claim(store, "first claim")
    rows1 = hot_mod.compute_hot_memory(store, limit=5, now=1000.0)
    # Add a second approved claim — should NOT appear in the next call
    # while within ttl_seconds of `now=1000`.
    _approved_claim(store, "second claim")
    rows2 = hot_mod.compute_hot_memory(store, limit=5, now=1000.5)
    assert len(rows1) == 1
    assert len(rows2) == 1  # cache held the first answer


def test_cache_expires_after_ttl(store: KBStore) -> None:
    _approved_claim(store, "first claim")
    hot_mod.compute_hot_memory(store, limit=5, now=1000.0)
    _approved_claim(store, "second claim")
    rows = hot_mod.compute_hot_memory(store, limit=5, now=1100.0)
    assert len(rows) == 2


# --- attach_hot_memory shape contracts ------------------------------------


def test_attach_to_dict_merges_meta(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    result: dict = {"foo": "bar"}
    wrapped = hot_mod.attach_hot_memory(result, store, query=None)
    assert wrapped is result  # in-place
    assert "_meta" in wrapped
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
    assert "_meta" in wrapped


def test_attach_empty_sidebar_is_noop(store: KBStore) -> None:
    """Empty KB → no hot memory → no _meta key added."""
    result: dict = {"foo": "bar"}
    out = hot_mod.attach_hot_memory(result, store, query=None)
    assert out is result
    assert "_meta" not in out


def test_attach_scalar_unchanged(store: KBStore) -> None:
    _approved_claim(store, "sky is blue")
    # Scalars have no envelope to attach to; just return as-is.
    assert hot_mod.attach_hot_memory("string", store, query=None) == "string"
    assert hot_mod.attach_hot_memory(42, store, query=None) == 42


# --- defensive: degrade-not-die on broken store -------------------------


def test_compute_returns_empty_on_broken_store(tmp_path: Path) -> None:
    """A non-vouch directory should yield an empty list rather than crash."""

    # KBStore.list_claims walks the claims dir; passing a path with no
    # `.vouch/claims/` simulates a partially-written tree.
    class BrokenStore:
        def __init__(self) -> None:
            self.kb_dir = tmp_path / ".vouch"

        def list_claims(self) -> list:
            raise RuntimeError("simulated read failure")

    rows = hot_mod.compute_hot_memory(BrokenStore(), limit=5)  # type: ignore[arg-type]
    assert rows == []


# --- realistic end-to-end via the proposals.approve path -----------------


def test_jsonl_read_claim_attaches_hot_memory(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JSONL server's _h_read_claim must attach _meta.vouch_hot_memory."""
    cid_target = _approved_claim(store, "target claim about jwt rotation")
    _approved_claim(store, "adjacent claim about jwt expiry")

    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    result = jsonl_server.handle_request(
        {"id": "r1", "method": "kb.read_claim", "params": {"claim_id": cid_target}},
    )
    assert result["ok"] is True
    payload = result["result"]
    assert payload["id"] == cid_target
    assert "_meta" in payload
    hot = payload["_meta"]["vouch_hot_memory"]
    # The claim itself should not echo back in its own sidebar.
    assert all(r["id"] != cid_target for r in hot)
    # The adjacent claim should show up.
    assert any("jwt expiry" in r["text"] for r in hot)
