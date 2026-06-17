"""Bidirectional vault sync — `vouch sync --vault <obsidian-dir>` (vouchdev/vouch#181).

``.vouch/pages/*.md`` is already plain markdown + YAML frontmatter, so an
Obsidian/Logseq vault is already a valid pages-tree. This module closes the
loop in both directions:

* **Forward (vault -> vouch).** When the human edits a previously-mirrored
  page in their vault, the next sync detects the diff, registers a
  ``vault:<relpath>`` source carrying the new bytes, and files a
  ``page-edit`` proposal in ``.vouch/proposed/`` so the existing review gate
  still gets the final say.
* **Backward (vouch -> vault).** Approved pages are mirrored into
  ``<vault>/vouch/pages/`` and approved claims surface as stub markdown
  files in ``<vault>/vouch/claims/`` so Obsidian can link to them via
  ``[[claim/<id>]]`` backlinks.

These tests cover the two directions independently, the orchestrator that
runs them together, the idempotency guarantee (re-running on an unchanged
tree must produce zero proposals), and the CLI surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.models import Claim, ClaimStatus, ClaimType, Page, PageStatus, PageType
from vouch.storage import KBStore
from vouch.vault_sync import (
    VAULT_DIR,
    VaultSyncError,
    VaultSyncResult,
    kb_to_vault,
    sync_vault,
    vault_to_kb,
)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    """Fresh KB with one source, one approved claim, one approved page."""
    s = KBStore.init(tmp_path / "project")
    src = s.put_source(b"seed", title="seed", locator="seed:1")
    claim = Claim(
        id="alpha-claim",
        text="The KB uses Obsidian-compatible markdown frontmatter.",
        type=ClaimType.FACT,
        status=ClaimStatus.ACTIONABLE,
        evidence=[src.id],
        approved_by="tester",
    )
    s.put_claim(claim)
    page = Page(
        id="alpha-page",
        title="Alpha page",
        body="# Alpha page\n\nOriginal body.\n",
        type=PageType.CONCEPT,
        status=PageStatus.ACTIVE,
        claims=[claim.id],
        sources=[src.id],
    )
    s.put_page(page)
    return s


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


# --- backward sync: vouch -> vault ---------------------------------------


def test_kb_to_vault_mirrors_approved_pages(store: KBStore, vault: Path) -> None:
    result = kb_to_vault(store, vault)
    page_file = vault / VAULT_DIR / "pages" / "alpha-page.md"
    assert page_file.is_file()
    body = page_file.read_text(encoding="utf-8")
    # Mirrored copy carries the on-disk frontmatter + body intact.
    assert "id: alpha-page" in body
    assert "title: Alpha page" in body
    assert "Original body." in body
    assert "alpha-page" in result.pages_mirrored


def test_kb_to_vault_creates_claim_stubs_with_backlinks(
    store: KBStore, vault: Path,
) -> None:
    """Approved claims become a markdown stub each so the vault can
    [[claim/...]]-backlink to them. The stub must cite the pages that
    reference the claim so Obsidian renders the graph."""
    kb_to_vault(store, vault)
    stub = vault / VAULT_DIR / "claims" / "alpha-claim.md"
    assert stub.is_file()
    text = stub.read_text(encoding="utf-8")
    assert "alpha-claim" in text
    assert "Obsidian-compatible" in text
    # The stub must mention the citing page so Obsidian's graph view links them.
    assert "alpha-page" in text


def test_kb_to_vault_skips_draft_pages(tmp_path: Path, vault: Path) -> None:
    """Mirror is for *approved* artifacts only -- a draft has not been
    through the review gate and must not leak into the vault."""
    s = KBStore.init(tmp_path / "kb")
    src = s.put_source(b"x", title="x")
    s.put_page(Page(
        id="draft-page", title="Draft",
        body="not yet approved", type=PageType.CONCEPT,
        status=PageStatus.DRAFT, sources=[src.id],
    ))
    result = kb_to_vault(s, vault)
    assert not (vault / VAULT_DIR / "pages" / "draft-page.md").exists()
    assert "draft-page" not in result.pages_mirrored


def test_kb_to_vault_is_idempotent(store: KBStore, vault: Path) -> None:
    kb_to_vault(store, vault)
    first = (vault / VAULT_DIR / "pages" / "alpha-page.md").read_text(encoding="utf-8")
    kb_to_vault(store, vault)
    second = (vault / VAULT_DIR / "pages" / "alpha-page.md").read_text(encoding="utf-8")
    assert first == second


# --- forward sync: vault -> vouch ----------------------------------------


def test_vault_to_kb_proposes_page_edit_on_changed_body(
    store: KBStore, vault: Path,
) -> None:
    """Acceptance #1: edit a mirrored page in Obsidian, run sync, see a
    page-edit proposal in .vouch/proposed/."""
    # Materialise the mirror first so the vault has the page.
    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    text = mirror.read_text(encoding="utf-8")
    edited = text.replace("Original body.", "Edited in Obsidian on a rainy night.")
    mirror.write_text(edited, encoding="utf-8")

    result = vault_to_kb(store, vault, actor="vault-sync")

    assert "alpha-page" in result.pages_proposed, result
    proposals = sorted((store.kb_dir / "proposed").glob("*.yaml"))
    assert proposals, "no proposal file was written"
    proposal_text = proposals[-1].read_text(encoding="utf-8")
    assert "alpha-page" in proposal_text
    assert "Edited in Obsidian on a rainy night." in proposal_text
    assert "vault-sync" in proposal_text


def test_vault_to_kb_creates_source_with_vault_locator(
    store: KBStore, vault: Path,
) -> None:
    """The proposal must cite a source whose locator is `vault:<relpath>`
    so a reviewer can trace the edit back to the vault file."""
    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    mirror.write_text(
        mirror.read_text(encoding="utf-8").replace("Original body.", "Edited."),
        encoding="utf-8",
    )

    vault_to_kb(store, vault, actor="vault-sync")
    sources = store.list_sources()
    vault_sources = [
        s for s in sources if s.locator and s.locator.startswith("vault:")
    ]
    locators = [s.locator for s in sources]
    assert vault_sources, f"no vault source registered; locators were {locators}"
    assert any("alpha-page.md" in (s.locator or "") for s in vault_sources)


def test_vault_to_kb_is_idempotent_when_nothing_changed(
    store: KBStore, vault: Path,
) -> None:
    """Acceptance: re-running sync on an unchanged tree produces zero
    proposals. Without this, every cron tick would spam the review queue."""
    kb_to_vault(store, vault)
    # First sync after mirror: nothing has changed.
    r1 = vault_to_kb(store, vault, actor="vault-sync")
    assert r1.pages_proposed == []
    # Second sync: still nothing.
    r2 = vault_to_kb(store, vault, actor="vault-sync")
    assert r2.pages_proposed == []


def test_vault_to_kb_ignores_files_outside_vouch_subdir(
    store: KBStore, vault: Path,
) -> None:
    """The user's own notes (outside `<vault>/vouch/`) are not vouch's
    business -- forward sync only watches the mirror subtree."""
    kb_to_vault(store, vault)
    (vault / "personal-note.md").write_text(
        "---\nid: personal-note\ntitle: Personal\n---\n\nThis is mine.\n",
        encoding="utf-8",
    )
    result = vault_to_kb(store, vault, actor="vault-sync")
    assert "personal-note" not in result.pages_proposed


def test_vault_to_kb_skips_when_no_mirror_dir_yet(
    store: KBStore, vault: Path,
) -> None:
    """First-ever forward sync on a never-mirrored vault must be benign."""
    result = vault_to_kb(store, vault, actor="vault-sync")
    assert isinstance(result, VaultSyncResult)
    assert result.pages_proposed == []


# --- sync_vault orchestrator ----------------------------------------------


def test_sync_vault_runs_both_directions_by_default(
    store: KBStore, vault: Path,
) -> None:
    result = sync_vault(store, vault)
    assert result.pages_mirrored == ["alpha-page"]
    # No vault edits yet -> no proposals.
    assert result.pages_proposed == []


def test_sync_vault_forward_only(store: KBStore, vault: Path) -> None:
    result = sync_vault(store, vault, direction="forward")
    # No mirror established, nothing to compare against.
    assert result.pages_mirrored == []
    assert result.pages_proposed == []


def test_sync_vault_backward_only(store: KBStore, vault: Path) -> None:
    result = sync_vault(store, vault, direction="backward")
    assert "alpha-page" in result.pages_mirrored
    assert (vault / VAULT_DIR / "pages" / "alpha-page.md").is_file()


def test_sync_vault_rejects_unknown_direction(store: KBStore, vault: Path) -> None:
    with pytest.raises(VaultSyncError, match="direction"):
        sync_vault(store, vault, direction="sideways")


def test_sync_vault_rejects_missing_vault(store: KBStore, tmp_path: Path) -> None:
    with pytest.raises(VaultSyncError, match="vault"):
        sync_vault(store, tmp_path / "does-not-exist")


# --- CLI surface ----------------------------------------------------------


def test_cli_sync_writes_mirror_on_first_run(
    store: KBStore, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    result = CliRunner().invoke(cli, ["sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert (vault / VAULT_DIR / "pages" / "alpha-page.md").is_file()
    assert "alpha-page" in result.output


def test_cli_sync_files_proposal_on_vault_edit(
    store: KBStore, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    runner = CliRunner()
    # First run mirrors only (nothing in vault to compare).
    runner.invoke(cli, ["sync", "--vault", str(vault)])
    # User edits the mirrored page.
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    mirror.write_text(
        mirror.read_text(encoding="utf-8").replace("Original body.", "Edited!"),
        encoding="utf-8",
    )
    # Second run picks up the edit and files a proposal.
    second = runner.invoke(cli, ["sync", "--vault", str(vault)])
    assert second.exit_code == 0, second.output
    proposals = sorted((store.kb_dir / "proposed").glob("*.yaml"))
    assert proposals, "no proposal filed by CLI"


def test_cli_sync_missing_vault_is_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    result = CliRunner().invoke(cli, ["sync", "--vault", "/nonexistent/path/here"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Error:" in result.output


def test_cli_sync_requires_vault_flag(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    result = CliRunner().invoke(cli, ["sync"])
    assert result.exit_code != 0
    assert any(w in result.output.lower() for w in ("--vault", "vault is required", "missing"))


# --- state-file behaviour (idempotency engine) ----------------------------


def test_state_file_lives_under_vouch_subdir(store: KBStore, vault: Path) -> None:
    sync_vault(store, vault)
    state = vault / VAULT_DIR / ".sync-state.json"
    assert state.is_file()
    data = json.loads(state.read_text(encoding="utf-8"))
    # Schema: a mapping from "pages/<id>.md" -> sha256 of the mirrored content
    # so the next forward pass can detect user edits.
    assert isinstance(data, dict)
    assert any(k.startswith("pages/") for k in data)


# --- fix #219: slug_hint, ghost-page guard, dedup proposals, claim stub warning ---


def test_vault_to_kb_proposal_uses_page_id_not_slugified_title(
    store: KBStore, vault: Path,
) -> None:
    """Fix 3 (#219): the proposal id must match the page id from frontmatter,
    not a slugified copy of the title. A page with id "alpha-page" and title
    "Alpha page" must produce a proposal targeting "alpha-page", not
    "alpha-page" derived from slugify("Alpha page") by accident -- but more
    importantly, a page whose id and title diverge (e.g. id="auth-001",
    title="Auth Decision") must still target the correct id."""
    # Add a page whose id and title diverge so slugify would produce the wrong id.
    src = store.put_source(b"extra", title="extra")
    from vouch.models import Claim as _Claim
    claim2 = _Claim(
        id="auth-claim",
        text="Auth claim.",
        evidence=[src.id],
        approved_by="tester",
    )
    store.put_claim(claim2)
    from vouch.models import Page as _Page
    from vouch.models import PageStatus, PageType
    page2 = _Page(
        id="auth-001",
        title="Auth Decision",
        body="Original auth body.\n",
        type=PageType.CONCEPT,
        status=PageStatus.ACTIVE,
        claims=[claim2.id],
        sources=[src.id],
    )
    store.put_page(page2)

    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "auth-001.md"
    mirror.write_text(
        mirror.read_text(encoding="utf-8").replace("Original auth body.", "Edited auth body."),
        encoding="utf-8",
    )

    result = vault_to_kb(store, vault, actor="vault-sync")

    assert "auth-001" in result.pages_proposed
    proposals = sorted((store.kb_dir / "proposed").glob("*.yaml"))
    proposal_text = proposals[-1].read_text(encoding="utf-8")
    # The proposal must target the original id, not "auth-decision"
    assert "id: auth-001" in proposal_text
    assert "id: auth-decision" not in proposal_text


def test_vault_to_kb_skips_ghost_page_after_kb_deletion(
    store: KBStore, vault: Path,
) -> None:
    """Fix 1 (#219): if a page is deleted from the KB after the last backward
    sync, vault_to_kb must skip the mirror file instead of filing a proposal
    for a non-existent page that would fail on approve."""
    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    # Simulate user edit so it looks like a changed file.
    mirror.write_text(
        mirror.read_text(encoding="utf-8").replace("Original body.", "Edited body."),
        encoding="utf-8",
    )
    # Delete the page from the KB to simulate post-mirror deletion.
    (store.kb_dir / "pages" / "alpha-page.md").unlink()

    result = vault_to_kb(store, vault, actor="vault-sync")

    assert "alpha-page" not in result.pages_proposed
    assert any("alpha-page" in s for s in result.pages_skipped_unknown_id)
    assert not list((store.kb_dir / "proposed").glob("*.yaml")), (
        "no proposal should be filed for a deleted page"
    )


def test_vault_to_kb_deduplicates_pending_proposals(
    store: KBStore, vault: Path,
) -> None:
    """Fix 2 (#219): running vault_to_kb twice before the first proposal is
    approved must not produce duplicate proposals for the same page."""
    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    edited_text = mirror.read_text(encoding="utf-8").replace(
        "Original body.", "Edited body."
    )
    mirror.write_text(edited_text, encoding="utf-8")

    # First run: proposal is filed.
    r1 = vault_to_kb(store, vault, actor="vault-sync")
    assert "alpha-page" in r1.pages_proposed
    proposals_after_first = list((store.kb_dir / "proposed").glob("*.yaml"))
    assert len(proposals_after_first) == 1

    # Restore the mirror to the edited state (simulate another sync tick
    # before the proposal is approved).
    mirror.write_text(edited_text, encoding="utf-8")
    # Also restore the state file so it still sees the edit.
    from vouch.vault_sync import _load_state, _save_state, _sha256_text
    state = _load_state(vault)
    # Revert state hash to force re-detection.
    state["pages/alpha-page.md"] = _sha256_text("old content")
    _save_state(vault, state)

    # Second run: must skip, not file a second proposal.
    r2 = vault_to_kb(store, vault, actor="vault-sync")
    assert "alpha-page" not in r2.pages_proposed
    proposals_after_second = list((store.kb_dir / "proposed").glob("*.yaml"))
    assert len(proposals_after_second) == 1, (
        f"expected 1 proposal after second run, got {len(proposals_after_second)}"
    )


def test_vault_to_kb_warns_on_claim_stub_edit(
    store: KBStore, vault: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Fix 4 (#219): editing a claim stub in the vault must produce a warning
    instead of being silently dropped, so the user knows claim stubs are
    read-only and they should edit the citing page instead."""
    import logging
    kb_to_vault(store, vault)
    stub = vault / VAULT_DIR / "claims" / "alpha-claim.md"
    assert stub.is_file(), "claim stub must exist after backward sync"

    # Simulate user editing the claim stub.
    stub.write_text(
        stub.read_text(encoding="utf-8") + "\n\n<!-- user edit -->",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="vouch.vault_sync"):
        result = vault_to_kb(store, vault, actor="vault-sync")

    assert "alpha-claim" not in result.pages_proposed
    assert any("claim stub" in record.message for record in caplog.records), (
        "expected a warning about claim stub edit, got: "
        + str([r.message for r in caplog.records])
    )
    assert any("alpha-claim" in s for s in result.claim_stubs_edited), (
        "expected alpha-claim.md in claim_stubs_edited"
    )


def test_vault_edit_proposal_can_be_approved_without_page_already_exists_error(
    store: KBStore, vault: Path,
) -> None:
    """P1 fix (#219): approving a vault-edit proposal must update the existing
    page rather than raising 'page already exists'. Before the fix, slug_hint=
    page_id caused _ensure_no_existing_artifact to reject every vault edit."""
    from vouch.proposals import approve
    kb_to_vault(store, vault)
    mirror = vault / VAULT_DIR / "pages" / "alpha-page.md"
    mirror.write_text(
        mirror.read_text(encoding="utf-8").replace("Original body.", "Approved edit."),
        encoding="utf-8",
    )

    result = vault_to_kb(store, vault, actor="vault-sync")
    assert "alpha-page" in result.pages_proposed

    proposals = sorted((store.kb_dir / "proposed").glob("*.yaml"))
    assert proposals, "no proposal was filed"
    proposal_id = proposals[-1].stem

    # Approving must succeed and update the existing page.
    approved = approve(store, proposal_id, approved_by="reviewer")
    assert approved.id == "alpha-page"
    updated = store.get_page("alpha-page")
    assert "Approved edit." in updated.body
