"""Scope stamping at write time + viewer-filtered recall surfaces.

Captured knowledge must record which project it belongs to when it lands —
scope cannot be retrofitted once KBs start sharing artifacts — and every
recall surface (including the SessionStart digest) must honour it.

The stamp and the read-side viewer resolve through ONE chain
(``scoping.viewer_from``), terminating in the durable ``kb.id`` — never the
mutable display name — so what a KB writes it can always read back, across
renames included.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vouch import proposals, recall
from vouch.models import Claim, Visibility
from vouch.proposals import ProposalError
from vouch.scoping import ViewerContext, viewer_from
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path / "myproj")


def _kb_project(store: KBStore) -> str:
    """The stamp/viewer project string: the KB's durable instance id.

    The id, not the display name — kb.name is mutable, and a rename must
    never orphan previously stamped artifacts (review finding).
    """
    identity = store.identity()
    assert identity is not None
    return identity[0]


# --- stamping at the propose gate ------------------------------------------


def test_propose_claim_stamps_own_project_scope(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="deploys run on tuesdays", evidence=[src.id], proposed_by="agent-a"
    )
    assert result.proposal.payload["scope"] == {
        "visibility": "project",
        "project": _kb_project(store),
    }
    claim = proposals.approve(store, result.proposal.id, approved_by="human")
    assert isinstance(claim, Claim)
    assert claim.scope.visibility is Visibility.PROJECT
    assert claim.scope.project == _kb_project(store)


def test_explicit_scope_wins_over_default(store: KBStore) -> None:
    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="private note", evidence=[src.id], proposed_by="agent-a",
        scope={"visibility": "private", "agent": "agent-a"},
    )
    assert result.proposal.payload["scope"]["visibility"] == "private"


def test_malformed_explicit_scope_is_refused_at_the_gate(store: KBStore) -> None:
    """A bad scope must fail at propose time as a ProposalError — filed
    unvalidated it would crash every audit read surface and escape
    approve() as a raw pydantic error (review finding)."""
    src = store.put_source(b"evidence")
    with pytest.raises(ProposalError):
        proposals.propose_claim(
            store, text="a fact", evidence=[src.id], proposed_by="agent-a",
            scope={"visibility": "globl"},
        )


def test_audit_read_survives_malformed_on_disk_scope(store: KBStore) -> None:
    """Defense in depth: a malformed scope already in a payload on disk
    (older writer, hand edit) degrades to unscoped, never a crash."""
    from vouch import audit
    from vouch.scoping import artifact_scope_for_object_id

    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="a fact", evidence=[src.id], proposed_by="agent-a"
    )
    proposal = store.get_proposal(result.proposal.id)
    proposal.payload["scope"] = {"visibility": "globl"}
    store.update_proposal(proposal)
    assert artifact_scope_for_object_id(store, proposal.id) is None
    viewer = ViewerContext(project="anything")
    events = list(audit.read_events(store.kb_dir, store=store, viewer=viewer))
    assert events  # did not raise


def test_propose_page_stamps_and_roundtrips(store: KBStore) -> None:
    proposal = proposals.propose_page(
        store, title="session notes", body="what happened", proposed_by="agent-a"
    )
    assert proposal.payload["scope"]["project"] == _kb_project(store)
    page = proposals.approve(store, proposal.id, approved_by="human")
    # the durable, re-deserialized page (frontmatter roundtrip) keeps the stamp
    durable = store.get_page(page.id)
    assert durable.scope.visibility is Visibility.PROJECT
    assert durable.scope.project == _kb_project(store)


def test_pre_identity_kb_stamps_nothing(store: KBStore) -> None:
    """A KB without a minted identity behaves exactly as before."""
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    del cfg["kb"]
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="a fact", evidence=[src.id], proposed_by="agent-a"
    )
    assert "scope" not in result.proposal.payload


def test_captured_answer_source_is_stamped(store: KBStore) -> None:
    from vouch import capture

    transcript = store.root / "transcript.jsonl"
    answer = (
        "The deploy pipeline runs on tuesdays. It is triggered by the ci "
        "scheduler and publishes to the internal registry."
    )
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": "how do deploys work?"}})
        + "\n"
        + json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    result = capture.capture_answer(store, "sess-scope", transcript, min_answer_chars=40)
    assert result.get("captured"), result
    source = store.get_source(result["source"])
    assert source.scope.project == _kb_project(store)
    # and the extracted claims carry the stamp through the gate
    stamped = [c for c in store.list_claims() if c.scope.project == _kb_project(store)]
    assert stamped


def test_ingested_source_is_stamped(store: KBStore) -> None:
    from vouch import extract

    src, _claims = extract.ingest_source(
        store, b"Some document text worth keeping around.", proposed_by="agent-a"
    )
    assert src.scope.visibility is Visibility.PROJECT
    assert src.scope.project == _kb_project(store)


# --- viewer defaults --------------------------------------------------------


def test_viewer_defaults_to_own_kb_id(store: KBStore) -> None:
    viewer = viewer_from(config_path=store.config_path)
    assert viewer.project == _kb_project(store)


def test_env_and_config_still_beat_own_id(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOUCH_PROJECT", "explicit-project")
    assert viewer_from(config_path=store.config_path).project == "explicit-project"


def test_retrieval_scope_config_beats_own_id(store: KBStore) -> None:
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg["retrieval"] = {**cfg.get("retrieval", {}), "scope": {"project": "cfg-project"}}
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert viewer_from(config_path=store.config_path).project == "cfg-project"


def test_stamp_follows_the_viewer_chain(store: KBStore) -> None:
    """Stamp and viewer share one resolver: with retrieval.scope set, new
    writes are stamped with that project, so they stay visible to the
    KB's own configured viewer (review finding: split-brain)."""
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg["retrieval"] = {**cfg.get("retrieval", {}), "scope": {"project": "cfg-project"}}
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="a fact", evidence=[src.id], proposed_by="agent-a"
    )
    assert result.proposal.payload["scope"]["project"] == "cfg-project"


def test_rename_does_not_orphan_stamped_knowledge(store: KBStore) -> None:
    """kb.name is display-only: renaming it must not hide stamped claims
    (review finding: the stamp is the durable kb.id)."""
    src = store.put_source(b"evidence")
    result = proposals.propose_claim(
        store, text="rename-proof fact", evidence=[src.id], proposed_by="agent-a"
    )
    proposals.approve(store, result.proposal.id, approved_by="human")
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg["kb"]["name"] = "myproj-renamed"
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert "rename-proof fact" in recall.build_digest(store)


# --- digest filtering -------------------------------------------------------


def _put_claim(store: KBStore, cid: str, text: str, scope: dict | None) -> None:
    src = store.put_source(f"src for {cid}".encode())
    kwargs = {"scope": scope} if scope is not None else {}
    store.put_claim(Claim(id=cid, text=text, evidence=[src.id], **kwargs))


def test_digest_hides_foreign_project_claims(store: KBStore) -> None:
    own = _kb_project(store)
    _put_claim(store, "own-fact", "our own fact", {"visibility": "project", "project": own})
    _put_claim(store, "unscoped-fact", "a legacy unscoped fact", None)
    _put_claim(
        store, "foreign-fact", "another project's fact",
        {"visibility": "project", "project": "other-kb"},
    )
    stats: dict[str, int] = {}
    digest = recall.build_digest(store, stats=stats)
    assert "our own fact" in digest
    assert "a legacy unscoped fact" in digest  # legacy data keeps working
    assert "another project's fact" not in digest
    assert stats["hidden"] == 1  # filtering is reported, never silent


def test_digest_explicit_viewer_override(store: KBStore) -> None:
    _put_claim(
        store, "foreign-fact", "another project's fact",
        {"visibility": "project", "project": "other-kb"},
    )
    digest = recall.build_digest(store, viewer=ViewerContext(project="other-kb"))
    assert "another project's fact" in digest


def test_digest_filters_pages_too(store: KBStore) -> None:
    from vouch.models import Page

    store.put_page(
        Page(id="own-page", title="our page", scope={"visibility": "project",
                                                     "project": _kb_project(store)})
    )
    store.put_page(
        Page(id="foreign-page", title="their page", scope={"visibility": "project",
                                                           "project": "other-kb"})
    )
    digest = recall.build_digest(store)
    assert "our page" in digest
    assert "their page" not in digest


def test_legacy_stray_page_scope_string_is_tolerated(store: KBStore) -> None:
    """Hand-edited frontmatter like `scope: whatever` must not make the
    page unreadable or hidden — it degrades to unscoped (review finding)."""
    import re

    from vouch.models import Page

    store.put_page(Page(id="legacy-page", title="legacy page"))
    path = store._page_path("legacy-page")
    raw = path.read_text(encoding="utf-8")
    patched = re.sub(r"scope:\n(  .+\n)+", "scope: not-a-visibility\n", raw)
    assert patched != raw
    path.write_text(patched, encoding="utf-8")
    page = store.get_page("legacy-page")
    assert page.scope.project is None
    assert "legacy page" in recall.build_digest(store)


# --- retrieval surface ------------------------------------------------------


def test_search_hides_foreign_project_claims(store: KBStore) -> None:
    from vouch import health
    from vouch.context import search_kb

    own = _kb_project(store)
    _put_claim(store, "own-fact", "tuesday deploys happen here",
               {"visibility": "project", "project": own})
    _put_claim(store, "foreign-fact", "tuesday deploys happen elsewhere",
               {"visibility": "project", "project": "other-kb"})
    health.rebuild_index(store)
    hits = search_kb(store, query="tuesday deploys")
    ids = [h["id"] for h in hits["hits"]]
    assert "own-fact" in ids
    assert "foreign-fact" not in ids
