"""Markdown-folder importer -- receipt-backed claims, state-tracked runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import md_import
from vouch.cli import cli
from vouch.models import ProposalKind, ProposalStatus
from vouch.storage import KBStore

DOC = (
    "# acme kickoff\n\n"
    "the acme-example launch moved to june. sarah-example owns the checklist.\n"
    "the rollout playbook lives in the shared vault under acme/rollout.md.\n"
)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    (tmp_path / "vault").mkdir()
    return s


def _note(store: KBStore, rel: str, text: str = DOC) -> Path:
    path = store.root / "vault" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _gate_on(store: KBStore) -> None:
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: true\n", encoding="utf-8"
    )


def _gate_off(store: KBStore) -> None:
    # kb init turns the receipt gate on; write the off state explicitly so
    # these tests pin the pending path no matter the repo default.
    store.config_path.write_text(
        "review:\n  auto_approve_on_receipt: false\n", encoding="utf-8"
    )


def _pending_claims(store: KBStore) -> list:
    return [
        p
        for p in store.list_proposals(ProposalStatus.PENDING)
        if p.kind == ProposalKind.CLAIM
    ]


def test_import_registers_sources_and_files_claims(store: KBStore) -> None:
    _gate_off(store)
    _note(store, "acme/plan.md")
    # distinct content per file: put_source is content-addressed, so two
    # identical files would legitimately collapse into one source
    _note(
        store,
        "memo.md",
        "# memo\n\n"
        "the infra-example migration finished without downtime on tuesday.\n"
        "backups now run hourly and the retention window is fourteen days.\n",
    )

    report = md_import.import_folder(store, store.root / "vault", actor="test-actor")

    assert report["files"] == 2
    assert report["ingested"] == 2
    assert report["approved"] == 0  # gate explicitly off: nothing bypasses review
    sources = store.list_sources()
    assert len(sources) == 2
    pending = _pending_claims(store)
    assert pending, "each file should have filed receipt-backed claims"
    assert all(p.proposed_by == "test-actor" for p in pending)


def test_import_auto_approves_only_under_gate(store: KBStore) -> None:
    _gate_on(store)
    _note(store, "plan.md")

    report = md_import.import_folder(store, store.root / "vault", actor="test-actor")

    assert report["approved"] > 0
    assert _pending_claims(store) == []
    assert store.list_claims(), "approved claims must be durable"


def test_import_seen_state_skips_unchanged_reattempts_edited(store: KBStore) -> None:
    path = _note(store, "plan.md")

    first = md_import.import_folder(store, store.root / "vault", actor="test-actor")
    second = md_import.import_folder(store, store.root / "vault", actor="test-actor")
    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert second["skipped"] == 1

    path.write_text(DOC + "\nnew paragraph with more substance to it.\n", encoding="utf-8")
    third = md_import.import_folder(store, store.root / "vault", actor="test-actor")
    assert third["ingested"] == 1


def test_import_skips_short_files_and_non_markdown(store: KBStore) -> None:
    _note(store, "tiny.md", "hi")
    _note(store, "note.txt")

    report = md_import.import_folder(store, store.root / "vault", actor="test-actor")

    assert report["ingested"] == 0
    assert report["files"] == 1, "only *.md files are walked"
    assert report["rows"][0]["reason"] == "too-short"
    assert store.list_sources() == []


def test_import_recurses_and_skips_hidden_dirs(store: KBStore) -> None:
    _note(store, "a/b/c.md")
    _note(store, ".obsidian/config.md")
    _note(store, ".git/keep.md")

    report = md_import.import_folder(store, store.root / "vault", actor="test-actor")

    assert report["files"] == 1
    assert report["rows"][0]["path"] == "a/b/c.md"
    assert len(store.list_sources()) == 1
    assert store.list_sources()[0].title == "a/b/c.md"


def test_import_writes_state_sidecar(store: KBStore) -> None:
    path = _note(store, "plan.md")

    md_import.import_folder(store, store.root / "vault", actor="test-actor")

    state_path = store.kb_dir / md_import.STATE_FILENAME
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert str(path.resolve()) in state
    assert all(isinstance(v, str) and len(v) == 64 for v in state.values())


def test_import_max_claims_bounds_density(store: KBStore) -> None:
    _gate_off(store)
    _note(store, "plan.md")

    report = md_import.import_folder(
        store, store.root / "vault", actor="test-actor", max_claims=1
    )

    assert report["ingested"] == 1
    assert len(_pending_claims(store)) == 1


def test_cli_import_md(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate_on(store)
    _note(store, "plan.md")
    monkeypatch.chdir(store.root)

    result = CliRunner().invoke(cli, ["import-md", "vault"])

    assert result.exit_code == 0, result.output
    assert "1 markdown file(s)" in result.output
    assert "auto-approved" in result.output
    assert (store.kb_dir / md_import.STATE_FILENAME).exists()


def test_cli_import_md_json(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _note(store, "plan.md")
    monkeypatch.chdir(store.root)

    result = CliRunner().invoke(cli, ["import-md", "vault", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["files"] == 1
    assert report["ingested"] == 1
    assert report["rows"][0]["path"] == "plan.md"


def test_md_import_never_imports_approve() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(md_import))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert "vouch.lifecycle" not in {i.rsplit(".", 1)[0] for i in imported}
    assert not any(name.endswith(".approve") for name in imported)


def test_cli_import_md_no_approve_pending(store, monkeypatch):
    """Cover pending-claims row display (+{approved} line + 'run vouch review').
    These lines are not hit when auto_approve_on_receipt is on."""
    _gate_off(store)
    _note(store, "plan.md")
    monkeypatch.chdir(store.root)

    result = CliRunner().invoke(cli, ["import-md", "vault", "--no-approve"])

    assert result.exit_code == 0, result.output
    assert "plan.md" in result.output
    assert "pending" in result.output
    assert "run" in result.output and "vouch review" in result.output




def test_load_state_non_dict(store, monkeypatch):
    """Corrupt state file (JSON list) triggers the non-dict return {} guard."""
    monkeypatch.chdir(store.root)
    state_file = md_import._state_path(store)
    state_file.write_text("[]", encoding="utf-8")
    assert md_import._load_state(store) == {}


def test_cli_import_md_second_run_skips(store, monkeypatch):
    """Second CLI run over unchanged notes hits the skipped-row continue path."""
    _gate_off(store)
    _note(store, "plan.md")
    monkeypatch.chdir(store.root)

    runner = CliRunner()
    first = runner.invoke(cli, ["import-md", "vault", "--no-approve"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, ["import-md", "vault", "--no-approve"])
    assert second.exit_code == 0, second.output
    assert "skipped 1" in second.output
