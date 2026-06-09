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
