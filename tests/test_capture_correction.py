"""Correction-capture — propose-only invariant, per-session cap, dedup, gating.

The load-bearing test is `test_capture_is_propose_only`: a detected correction
must land as a PENDING claim proposal and nothing may become approved. The
module has no call path to `proposals.approve`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from vouch import correction
from vouch.models import ProposalKind, ProposalStatus
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    """A fresh KB rooted at a temp dir."""
    return KBStore.init(tmp_path)


# --- detection heuristic --------------------------------------------------


@pytest.mark.parametrize("prompt", [
    "no, we deploy from main not release",
    "No. that's the staging bucket, prod is us-west-2",
    "actually, the retry limit is 5 not 3",
    "that's wrong — the service owner is the platform team",
    "we don't use redis for sessions anymore",
    "nope, the endpoint is /v2/users",
])
def test_detect_correction_positive(prompt: str) -> None:
    """Pushback openers and corrective phrases are detected."""
    assert correction.detect_correction(prompt) is not None


@pytest.mark.parametrize("prompt", [
    "please add a test for the parser",
    "can you deploy from main?",
    "no idea why the build is red, can you look?",
    "",
    "   ",
])
def test_detect_correction_negative(prompt: str) -> None:
    """Ordinary instructions and questions are not treated as corrections."""
    assert correction.detect_correction(prompt) is None


def test_detect_strips_leading_marker() -> None:
    """The captured text quotes the correction, not the pushback marker."""
    assert correction.detect_correction("no, we deploy from main") == "we deploy from main"
    assert correction.detect_correction("actually, prod is us-west-2") == "prod is us-west-2"


# --- propose-only invariant -----------------------------------------------


def test_capture_is_propose_only(store: KBStore) -> None:
    """A correction enqueues a PENDING claim; nothing is approved."""
    result = correction.capture_correction(
        store, prompt="no, we deploy from main not release", session_id="s1",
    )
    assert result["captured"] is True

    pending = store.list_proposals(status=ProposalStatus.PENDING)
    assert len(pending) == 1
    prop = pending[0]
    assert prop.kind == ProposalKind.CLAIM
    assert prop.proposed_by == correction.CORRECTION_ACTOR
    assert prop.status == ProposalStatus.PENDING
    assert prop.rationale == "captured from user correction"
    assert correction.CORRECTION_TAG in prop.payload.get("tags", [])
    assert prop.payload["text"] == "we deploy from main not release"

    # The claim cites the registered correction message as evidence.
    assert prop.payload["evidence"] == [result["source_id"]]
    assert store.get_source(result["source_id"]).type == "message"

    # Nothing crossed the review gate.
    assert store.list_claims() == []
    assert store.list_proposals(status=ProposalStatus.APPROVED) == []


def test_module_has_no_approve_path() -> None:
    """Structural guard: the module never imports or calls proposals.approve."""
    tree = ast.parse(inspect.getsource(correction))
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute)
        else getattr(node.func, "id", None)
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "approve" not in imported
    assert "approve" not in called


# --- per-session cap ------------------------------------------------------


def test_per_session_cap_bounds_auto_proposals(store: KBStore) -> None:
    """Distinct corrections stop being filed once the session hits the cap."""
    cfg = correction.CorrectionConfig(enabled=True, per_session_cap=2)
    for i in range(5):
        correction.capture_correction(
            store, prompt=f"no, fact number {i} is the real value",
            session_id="s1", config=cfg,
        )
    pending = store.list_proposals(status=ProposalStatus.PENDING)
    assert len(pending) == 2

    # The overflow is reported as a cap skip, not an error.
    over = correction.capture_correction(
        store, prompt="no, one more distinct correction here",
        session_id="s1", config=cfg,
    )
    assert over["captured"] is False
    assert over["skipped"] == "session-cap"


def test_cap_is_per_session(store: KBStore) -> None:
    """A different session gets its own budget."""
    cfg = correction.CorrectionConfig(enabled=True, per_session_cap=1)
    correction.capture_correction(
        store, prompt="no, alpha is the value", session_id="s1", config=cfg)
    blocked = correction.capture_correction(
        store, prompt="no, beta is the value", session_id="s1", config=cfg)
    assert blocked["skipped"] == "session-cap"

    other = correction.capture_correction(
        store, prompt="no, gamma is the value", session_id="s2", config=cfg)
    assert other["captured"] is True


# --- dedup ----------------------------------------------------------------


def test_duplicate_correction_suppressed(store: KBStore) -> None:
    """An exact-repeat correction is suppressed (embeddings-free path)."""
    first = correction.capture_correction(
        store, prompt="no, we deploy from main", session_id="s1")
    assert first["captured"] is True

    second = correction.capture_correction(
        store, prompt="no, we deploy from main", session_id="s1")
    assert second["captured"] is False
    assert second["skipped"] == "duplicate"

    assert len(store.list_proposals(status=ProposalStatus.PENDING)) == 1


# --- config gate ----------------------------------------------------------


def test_disabled_via_config(store: KBStore) -> None:
    """capture.correction.enabled: false makes capture a no-op."""
    store.config_path.write_text(
        "capture:\n  correction:\n    enabled: false\n", encoding="utf-8")
    result = correction.capture_correction(
        store, prompt="no, this is a correction", session_id="s1")
    assert result["captured"] is False
    assert result["skipped"] == "disabled"
    assert store.list_proposals(status=ProposalStatus.PENDING) == []


def test_load_config_defaults(store: KBStore) -> None:
    """Absent config yields enabled=True with the default cap."""
    cfg = correction.load_config(store)
    assert cfg.enabled is True
    assert cfg.per_session_cap == correction.DEFAULT_PER_SESSION_CAP


def test_load_config_reads_override(store: KBStore) -> None:
    """The capture.correction block overrides the defaults."""
    store.config_path.write_text(
        "capture:\n  correction:\n    enabled: false\n    per_session_cap: 7\n",
        encoding="utf-8")
    cfg = correction.load_config(store)
    assert cfg.enabled is False
    assert cfg.per_session_cap == 7


def test_non_correction_prompt_files_nothing(store: KBStore) -> None:
    """A normal instruction produces no proposal."""
    result = correction.capture_correction(
        store, prompt="please refactor the parser", session_id="s1")
    assert result["captured"] is False
    assert result["skipped"] == "no-correction"
    assert store.list_proposals() == []
