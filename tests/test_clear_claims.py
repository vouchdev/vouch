"""Test the bulk clear feature for auto-saved claims (issue #433)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from vouch import lifecycle as life
from vouch.embeddings import DEFAULT_MODEL_NAME, register
from vouch.proposals import approve, propose_claim
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _mock_embedder() -> None:
    # Mock embedder to avoid needing sentence_transformers
    pytest.importorskip("numpy")
    from tests.embeddings._fakes import MockEmbedder
    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    # Enable trusted-agent mode for auto-approval tests
    s.config_path.write_text("review:\n  approver_role: trusted-agent\n", encoding="utf-8")
    return s


def test_clear_claims_auto_only_filters_correctly(store: KBStore) -> None:
    """Only auto-approved claims are cleared when auto_only=True."""
    src = store.put_source(b"source1")

    # Create a claim proposed and auto-approved by 'agent'
    pr_auto = propose_claim(
        store, text="auto-approved claim", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr_auto.id, approved_by="agent")  # Same actor = auto-approved

    # Create a claim proposed by agent but approved by human
    pr_manual = propose_claim(
        store, text="manually-approved claim", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr_manual.id, approved_by="human")  # Different actor = manual

    # Clear only auto-approved claims
    cleared = life.clear_claims(store, auto_only=True, before=None, actor="user", dry_run=False)

    assert len(cleared) == 1
    assert cleared[0].text == "auto-approved claim"
    assert cleared[0].auto_approved is True

    # Verify the claim was archived
    archived_claim = store.get_claim(cleared[0].id)
    assert archived_claim.status.value == "archived"


def test_clear_claims_before_date_filters_correctly(store: KBStore) -> None:
    """Date filtering respects the before parameter."""
    src = store.put_source(b"source1")
    now = datetime.now(UTC)

    # Create an auto-approved claim
    pr1 = propose_claim(
        store, text="old claim", evidence=[src.id], proposed_by="agent"
    )
    claim1 = approve(store, pr1.id, approved_by="agent")
    old_claim = store.get_claim(claim1.id)

    # Manually set created_at to 2 days ago
    old_claim.created_at = now - timedelta(days=2)
    store.update_claim(old_claim)

    # Create another auto-approved claim (now)
    pr2 = propose_claim(
        store, text="new claim", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr2.id, approved_by="agent")

    # Clear claims created before 1 day ago
    cutoff = now - timedelta(days=1)
    cleared = life.clear_claims(
        store, auto_only=True, before=cutoff, actor="user", dry_run=False
    )

    assert len(cleared) == 1
    assert cleared[0].text == "old claim"


def test_clear_claims_dry_run_does_not_modify(store: KBStore) -> None:
    """Dry-run returns what would be cleared but doesn't modify anything."""
    src = store.put_source(b"source1")

    # Create an auto-approved claim
    pr = propose_claim(
        store, text="claim to clear", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr.id, approved_by="agent")

    # Dry-run the clear
    cleared = life.clear_claims(
        store, auto_only=True, before=None, actor="user", dry_run=True
    )

    assert len(cleared) == 1

    # Verify the claim is NOT actually archived
    claim = store.get_claim(cleared[0].id)
    assert claim.status.value != "archived"


def test_clear_claims_skips_already_archived(store: KBStore) -> None:
    """Already-archived claims are not re-archived."""
    src = store.put_source(b"source1")

    # Create and approve a claim
    pr = propose_claim(
        store, text="claim", evidence=[src.id], proposed_by="agent"
    )
    claim = approve(store, pr.id, approved_by="agent")
    claim_id = claim.id

    # Archive it first
    life.archive(store, claim_id=claim_id, actor="user")

    # Try to clear it
    cleared = life.clear_claims(
        store, auto_only=True, before=None, actor="user", dry_run=False
    )

    # Should not be in the clear list since it's already archived
    assert len(cleared) == 0


def test_clear_claims_respects_auto_only_false(store: KBStore) -> None:
    """When auto_only=False, all matching claims are cleared."""
    src = store.put_source(b"source1")

    # Create auto-approved and manually-approved claims
    pr_auto = propose_claim(
        store, text="auto", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr_auto.id, approved_by="agent")

    pr_manual = propose_claim(
        store, text="manual", evidence=[src.id], proposed_by="agent"
    )
    approve(store, pr_manual.id, approved_by="human")

    # Clear with auto_only=False
    cleared = life.clear_claims(
        store, auto_only=False, before=None, actor="user", dry_run=False
    )

    assert len(cleared) == 2
    texts = {c.text for c in cleared}
    assert texts == {"auto", "manual"}


def test_clear_claims_logs_audit_event(store: KBStore) -> None:
    """Audit log records the bulk clear operation."""
    src = store.put_source(b"source1")

    # Create a few auto-approved claims
    for i in range(3):
        pr = propose_claim(
            store, text=f"claim{i}", evidence=[src.id], proposed_by="agent"
        )
        approve(store, pr.id, approved_by="agent")

    # Clear them
    cleared = life.clear_claims(
        store, auto_only=True, before=None, actor="test_user", dry_run=False
    )

    assert len(cleared) == 3

    # Check audit log
    from vouch import audit
    events = list(audit.read_events(store.kb_dir))
    clear_events = [e for e in events if e.event == "claim.bulk_clear"]

    assert len(clear_events) == 1
    event = clear_events[0]
    assert event.actor == "test_user"
    assert event.data["count"] == 3
    assert event.data["auto_only"] is True
    assert event.data["before"] is None
