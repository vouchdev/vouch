"""Conversation-export importers (vouchdev/vouch#431)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.corpus_import import (
    candidates_from_chat_json,
    candidates_from_markdown_vault,
    run_import,
)
from vouch.models import Claim, Page, PageStatus, PageType, ProposalStatus
from vouch.storage import KBStore

_SIMILAR = "Auth uses JWTs in the Authorization header."


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _install_mock_embedder() -> None:
    pytest.importorskip("numpy")
    from tests.embeddings._fakes import MockEmbedder
    from vouch.embeddings import register
    from vouch.embeddings.base import DEFAULT_MODEL_NAME

    register(DEFAULT_MODEL_NAME, lambda: MockEmbedder(dim=8))


def test_markdown_vault_proposes_pages_only(store: KBStore, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "alpha.md").write_text(
        "---\nid: alpha-page\ntitle: Alpha\n---\n\n# Alpha\n\nBody text.\n",
        encoding="utf-8",
    )
    (vault / "beta.md").write_text("# Beta\n\nNo frontmatter.\n", encoding="utf-8")

    result = run_import(store, "markdown-vault", vault, actor="tester")

    assert result.pages_proposed == 2
    assert result.claims_proposed == 0
    assert len(result.proposal_ids) == 2
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 2
    assert not list(store.list_pages())
    assert not list(store.list_claims())


def test_chat_json_short_messages_become_claims(store: KBStore, tmp_path: Path) -> None:
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps([
            {"role": "user", "content": "What is auth?"},
            {"role": "assistant", "content": "We use JWT bearer tokens."},
            {"role": "assistant", "content": "Refresh tokens rotate hourly."},
        ]),
        encoding="utf-8",
    )

    result = run_import(store, "chat-json", export, actor="tester")

    assert result.claims_proposed == 2
    assert result.pages_proposed == 0
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 2
    assert not store.list_claims()


def test_dry_run_reports_without_enqueuing(store: KBStore, tmp_path: Path) -> None:
    export = tmp_path / "mem.json"
    export.write_text(json.dumps(["fact one", "fact two"]), encoding="utf-8")

    result = run_import(store, "memory-export", export, dry_run=True, actor="tester")

    assert result.dry_run is True
    assert result.claims_proposed == 2
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_max_proposals_cap(store: KBStore, tmp_path: Path) -> None:
    export = tmp_path / "mem.json"
    export.write_text(
        json.dumps(["one", "two", "three", "four"]),
        encoding="utf-8",
    )

    result = run_import(
        store, "memory-export", export, max_proposals=2, actor="tester",
    )

    assert result.claims_proposed == 2
    assert result.cap_hit is True
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 2


def test_claim_dedup_skips_similar_approved(store: KBStore, tmp_path: Path) -> None:
    _install_mock_embedder()
    src = store.put_source(b"e")
    store.put_claim(Claim(id="auth-jwt", text=_SIMILAR, evidence=[src.id]))

    export = tmp_path / "mem.json"
    export.write_text(json.dumps([_SIMILAR, "Unrelated new fact."]), encoding="utf-8")

    result = run_import(store, "memory-export", export, actor="tester")

    assert result.claims_proposed == 1
    assert result.claims_skipped_dedup == 1
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].payload["text"] == "Unrelated new fact."


def test_page_dedup_skips_existing_slug(store: KBStore, tmp_path: Path) -> None:
    src = store.put_source(b"x", title="x")
    store.put_page(Page(
        id="alpha-page",
        title="Alpha",
        body="existing",
        type=PageType.CONCEPT,
        status=PageStatus.ACTIVE,
        sources=[src.id],
    ))

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "dup.md").write_text(
        "---\nid: alpha-page\ntitle: Alpha copy\n---\n\nNew body.\n",
        encoding="utf-8",
    )
    (vault / "fresh.md").write_text("---\ntitle: Fresh\n---\n\nNew page.\n", encoding="utf-8")

    result = run_import(store, "markdown-vault", vault, actor="tester")

    assert result.pages_proposed == 1
    assert result.pages_skipped_dedup == 1
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 1
    assert pending[0].payload["id"] == "fresh"


def test_candidates_from_chat_json_parses_messages_key(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({
            "title": "Session",
            "messages": [
                {"role": "assistant", "content": "Short answer."},
            ],
        }),
        encoding="utf-8",
    )
    cands = candidates_from_chat_json(path)
    assert len(cands) == 1
    assert cands[0].kind == "claim"
    assert cands[0].text == "Short answer."


def test_candidates_from_markdown_vault(tmp_path: Path) -> None:
    root = tmp_path / "v"
    root.mkdir()
    (root / "note.md").write_text("# Title\n\nBody.\n", encoding="utf-8")
    cands = candidates_from_markdown_vault(root)
    assert len(cands) == 1
    assert cands[0].kind == "page"
    assert cands[0].title == "note"


def test_cli_import_json_output(store: KBStore, tmp_path: Path) -> None:
    export = tmp_path / "chat.json"
    export.write_text(
        json.dumps([{"role": "assistant", "content": "CLI claim."}]),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["import", "chat-json", str(export), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claims_proposed"] == 1
    assert payload["pages_proposed"] == 0
    assert len(payload["proposal_ids"]) == 1
