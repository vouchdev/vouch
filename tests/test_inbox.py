"""Inbox folder importer — proposes, never approves."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import inbox
from vouch.cli import cli
from vouch.models import ProposalStatus
from vouch.storage import KBStore

DOC = "# meeting notes\n\nalice-example agreed to send the acme-example draft by friday.\n"


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    (tmp_path / "inbox").mkdir()
    return s


def _drop(store: KBStore, name: str, text: str = DOC) -> Path:
    path = store.root / "inbox" / name
    path.write_text(text, encoding="utf-8")
    return path


def test_scan_proposes_one_pending_page_per_file(store: KBStore) -> None:
    _drop(store, "notes.md")
    _drop(store, "memo.txt")

    result = inbox.scan(store, store.root / "inbox")

    assert len(result.proposed) == 2
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert {p.id for p in pending} == set(result.proposed)
    # every proposal cites the registered content-addressed source
    sources = {s.id for s in store.list_sources()}
    for p in pending:
        assert set(p.payload.get("sources", [])) <= sources
        assert p.payload.get("sources"), p.id
        assert p.proposed_by == "inbox"
    # nothing was approved: no durable pages beyond the starter seed
    assert all(pg.tags != ["inbox"] for pg in store.list_pages())


def test_scan_seen_state_dedups_and_rescans_changed_files(store: KBStore) -> None:
    path = _drop(store, "notes.md")

    first = inbox.scan(store, store.root / "inbox")
    second = inbox.scan(store, store.root / "inbox")
    assert len(first.proposed) == 1
    assert second.proposed == []

    path.write_text(DOC + "\nnew paragraph with more substance to it.\n", encoding="utf-8")
    third = inbox.scan(store, store.root / "inbox")
    assert len(third.proposed) == 1


def test_scan_skips_short_files_and_foreign_extensions(store: KBStore) -> None:
    _drop(store, "tiny.md", "hi")
    _drop(store, "binary.png", "not really a png but the extension rules")

    result = inbox.scan(store, store.root / "inbox")

    assert result.proposed == []
    assert sorted(result.skipped) == ["binary.png", "tiny.md"]


def test_scan_disabled_via_config_is_noop(store: KBStore) -> None:
    store.config_path.write_text(
        store.config_path.read_text(encoding="utf-8") + "\ninbox:\n  enabled: false\n",
        encoding="utf-8",
    )
    _drop(store, "notes.md")

    result = inbox.scan(store, store.root / "inbox")
    assert result.proposed == []
    assert store.list_proposals(ProposalStatus.PENDING) == []


def test_watch_is_bounded_by_iterations(store: KBStore) -> None:
    _drop(store, "notes.md")
    results: list[inbox.ScanResult] = []

    inbox.watch(
        store,
        store.root / "inbox",
        poll_interval=0.01,
        iterations=2,
        on_result=results.append,
    )

    assert len(results) == 2
    assert len(results[0].proposed) == 1
    assert results[1].proposed == []


def test_cli_inbox_single_pass(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _drop(store, "notes.md")
    monkeypatch.chdir(store.root)

    result = CliRunner().invoke(cli, ["inbox", "--dir", "inbox"])

    assert result.exit_code == 0, result.output
    assert "1 proposal(s)" in result.output
    assert len(store.list_proposals(ProposalStatus.PENDING)) == 1


def test_inbox_never_imports_approve() -> None:
    import ast
    import inspect

    from vouch import inbox as inbox_mod

    tree = ast.parse(inspect.getsource(inbox_mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert "vouch.lifecycle" not in {i.rsplit(".", 1)[0] for i in imported}
    assert not any(name.endswith(".approve") for name in imported)
