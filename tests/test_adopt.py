"""`vouch adopt` — draining personal-KB fallback captures into a project KB."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import adopt as adopt_mod
from vouch import audit, capture, hub
from vouch.cli import cli
from vouch.models import ClaimStatus, ProposalStatus
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path_factory, monkeypatch):
    """Fake $HOME + registry so tests never touch the real machine."""
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv(hub.REGISTRY_ENV, str(fake_home / "registry.yaml"))
    monkeypatch.delenv("VOUCH_KB_PATH", raising=False)
    monkeypatch.delenv("VOUCH_PROJECT_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv(hub.PERSONAL_KB_ENV, raising=False)
    return fake_home


ANSWER = (
    "The deploy cadence for this service is every second Tuesday. "
    "The staging environment refreshes nightly at 02:00 UTC. "
    "Rollbacks use the blue-green switch, never a re-deploy of an old tag."
)


@pytest.fixture()
def personal(tmp_path: Path) -> KBStore:
    root = hub.personal_kb_root()
    assert root is not None
    store = KBStore.init(root)
    hub.register_kb(root, role="personal", actor="t")
    hub.set_personal_fallback(root, True)
    return store


def _fallback_capture(personal: KBStore, origin: Path, tmp_path: Path) -> dict:
    """One captured session answer in ``origin``, routed to the personal KB."""
    transcript = tmp_path / f"transcript-{origin.name}.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "what is the deploy cadence?"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": ANSWER}]}},
    ]
    transcript.write_text(
        "\n".join(json.dumps(entry) for entry in lines), encoding="utf-8"
    )
    result = capture.capture_answer(
        personal, f"s-{origin.name}", transcript, origin=origin,
    )
    assert result["captured"] is True
    return result


def test_fallback_capture_stamps_origin(personal: KBStore, tmp_path: Path) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    result = _fallback_capture(personal, origin, tmp_path)
    src = personal.get_source(result["source"])
    assert src.metadata["origin_path"] == str(origin)
    assert "personal-fallback" in src.tags
    # a normal (non-fallback) capture carries no origin
    assert result["approved"] >= 1


def test_adopt_moves_claims_through_the_gate(
    personal: KBStore, tmp_path: Path
) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    captured = _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)

    report = adopt_mod.adopt(project, personal, match_root=origin)
    assert report.sources == [captured["source"]]
    assert len(report.claims_durable) == captured["approved"]
    assert report.claims_pending == []
    # durable in the project KB, stamped with the project's own scope
    for claim_id in report.claims_durable:
        claim = project.get_claim(claim_id)
        assert claim.scope.project == project.identity()[0]  # type: ignore[index]
        assert "adopted" in claim.tags
    # both audit logs record the adoption with the other side's id
    proj_events = [e for e in audit.read_events(project.kb_dir) if e.event == "kb.adopt"]
    per_events = [e for e in audit.read_events(personal.kb_dir) if e.event == "kb.adopt"]
    assert proj_events and proj_events[0].data["from_kb"] == personal.identity()[0]  # type: ignore[index]
    assert per_events and per_events[0].data["to_kb"] == project.identity()[0]  # type: ignore[index]


def test_adopt_is_idempotent(personal: KBStore, tmp_path: Path) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    first = adopt_mod.adopt(project, personal, match_root=origin)
    assert first.claims_durable
    again = adopt_mod.adopt(project, personal, match_root=origin)
    assert again.sources == []
    assert again.claims_durable == []
    assert again.claims_pending == []
    assert sorted(again.claims_skipped) == sorted(first.claims_durable)
    # a fully-skipped pass adds no second kb.adopt event
    proj_events = [e for e in audit.read_events(project.kb_dir) if e.event == "kb.adopt"]
    assert len(proj_events) == 1


def test_adopt_respects_a_closed_gate(personal: KBStore, tmp_path: Path) -> None:
    """A project whose review gate is human-only gets PENDING proposals."""
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    cfg = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    cfg["review"]["auto_approve_on_receipt"] = False
    project.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    report = adopt_mod.adopt(project, personal, match_root=origin)
    assert report.claims_durable == []
    assert report.claims_pending
    pending = project.list_proposals(ProposalStatus.PENDING)
    assert {p.payload["id"] for p in pending} >= set(report.claims_pending)


def test_adopt_ignores_other_origins(personal: KBStore, tmp_path: Path) -> None:
    origin_a = tmp_path / "projA"
    origin_b = tmp_path / "projB"
    origin_a.mkdir()
    origin_b.mkdir()
    _fallback_capture(personal, origin_b, tmp_path)
    project = KBStore.init(origin_a)
    report = adopt_mod.adopt(project, personal, match_root=origin_a)
    assert report.sources == []
    assert report.claims_durable == []
    # the starter claim (no origin) never travels either
    assert all("starter" not in cid for cid in report.claims_durable)


def test_adopt_skips_dead_personal_claims(personal: KBStore, tmp_path: Path) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    # archive one personal claim before adopting — it must not travel
    adoptable = [c for c in personal.list_claims() if "starter" not in c.id]
    victim = adoptable[0]
    from vouch import lifecycle

    lifecycle.archive(personal, claim_id=victim.id, actor="t")
    project = KBStore.init(origin)
    report = adopt_mod.adopt(project, personal, match_root=origin)
    assert victim.id not in report.claims_durable
    assert victim.id not in report.claims_pending


def test_adopt_retire_archives_personal_copies(
    personal: KBStore, tmp_path: Path
) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    report = adopt_mod.adopt(project, personal, match_root=origin, retire=True)
    assert sorted(report.retired) == sorted(report.claims_durable)
    for claim_id in report.retired:
        assert personal.get_claim(claim_id).status == ClaimStatus.ARCHIVED
    # the project copies stay durable
    for claim_id in report.claims_durable:
        assert project.get_claim(claim_id).status == ClaimStatus.WORKING


def test_adopt_dry_run_writes_nothing(personal: KBStore, tmp_path: Path) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    captured = _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    before = {c.id for c in project.list_claims()}
    report = adopt_mod.adopt(project, personal, match_root=origin, dry_run=True)
    assert report.sources == [captured["source"]]
    assert report.claims_durable  # candidates
    assert {c.id for c in project.list_claims()} == before
    assert not [e for e in audit.read_events(project.kb_dir) if e.event == "kb.adopt"]


def test_adopt_matches_subfolder_origins(personal: KBStore, tmp_path: Path) -> None:
    """A session captured in a subfolder of the project still belongs to it."""
    origin = tmp_path / "projA"
    sub = origin / "src" / "deep"
    sub.mkdir(parents=True)
    _fallback_capture(personal, sub, tmp_path)
    project = KBStore.init(origin)
    report = adopt_mod.adopt(project, personal, match_root=origin)
    assert report.sources
    assert report.claims_durable


# --- the CLI wrapper -------------------------------------------------------


def test_cli_adopt_end_to_end(personal: KBStore, tmp_path: Path, monkeypatch) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    KBStore.init(origin)
    monkeypatch.chdir(origin)
    runner = CliRunner()
    r = runner.invoke(cli, ["adopt", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "would adopt" in r.output
    r = runner.invoke(cli, ["adopt", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["claims_durable"]
    r = runner.invoke(cli, ["adopt"])
    assert r.exit_code == 0, r.output
    assert "skipped" in r.output


def test_cli_adopt_without_personal_kb(tmp_path: Path, monkeypatch) -> None:
    proj = tmp_path / "proj"
    KBStore.init(proj)
    monkeypatch.chdir(proj)
    r = CliRunner().invoke(cli, ["adopt"])
    assert r.exit_code != 0
    assert "init-personal" in r.output


def test_cli_adopt_refuses_inside_personal_kb(
    personal: KBStore, monkeypatch
) -> None:
    monkeypatch.chdir(personal.root)
    r = CliRunner().invoke(cli, ["adopt"])
    assert r.exit_code != 0
    assert "IS the personal KB" in r.output


def test_cli_adopt_nothing_to_adopt(personal: KBStore, tmp_path: Path, monkeypatch) -> None:
    proj = tmp_path / "clean"
    KBStore.init(proj)
    monkeypatch.chdir(proj)
    r = CliRunner().invoke(cli, ["adopt"])
    assert r.exit_code == 0, r.output
    assert "nothing to adopt" in r.output


def test_cli_adopt_from_path_override(
    personal: KBStore, tmp_path: Path, monkeypatch
) -> None:
    """A project that moved since capture adopts via --from-path."""
    old_home = tmp_path / "old-location"
    old_home.mkdir()
    _fallback_capture(personal, old_home, tmp_path)
    new_home = tmp_path / "new-location"
    KBStore.init(new_home)
    monkeypatch.chdir(new_home)
    runner = CliRunner()
    r = runner.invoke(cli, ["adopt"])
    assert "nothing to adopt" in r.output
    r = runner.invoke(cli, ["adopt", "--from-path", str(old_home)])
    assert r.exit_code == 0, r.output
    assert "adopted" in r.output


def test_adopt_does_not_requeue_pending_claims(
    personal: KBStore, tmp_path: Path
) -> None:
    """Under a human-only gate adopted claims land PENDING; a second pass must
    skip them, not file a duplicate proposal per run into the review queue."""
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    cfg = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    cfg["review"]["auto_approve_on_receipt"] = False
    project.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    first = adopt_mod.adopt(project, personal, match_root=origin)
    assert first.claims_pending
    queued_after_first = [
        p.payload["id"] for p in project.list_proposals(ProposalStatus.PENDING)
    ]
    second = adopt_mod.adopt(project, personal, match_root=origin)
    assert second.claims_pending == []
    assert sorted(second.claims_skipped) == sorted(first.claims_pending)
    queued_after_second = [
        p.payload["id"] for p in project.list_proposals(ProposalStatus.PENDING)
    ]
    assert sorted(queued_after_second) == sorted(queued_after_first)
    # dry-run agrees with the real run about what is left to do
    dry = adopt_mod.adopt(project, personal, match_root=origin, dry_run=True)
    assert dry.claims_durable == [] and dry.claims_pending == []


def test_personal_digest_says_it_is_machine_wide(
    personal: KBStore, tmp_path: Path
) -> None:
    """A KB-less folder reads the personal KB whole — the injected header must
    not claim the knowledge belongs to this repo."""
    from vouch import recall as recall_mod

    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project_style = recall_mod.build_digest(personal, personal=False)
    assert "for this repo" in project_style
    personal_style = recall_mod.build_digest(personal, personal=True)
    assert "for this repo" not in personal_style
    assert "personal vouch KB" in personal_style
    assert "shared across every KB-less folder" in personal_style


def test_personal_prompt_hook_names_the_personal_kb(
    personal: KBStore, tmp_path: Path
) -> None:
    from vouch import hooks

    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    stdin = json.dumps({"prompt": "what is the deploy cadence?", "session_id": "s1"})
    project_style = hooks.build_claude_prompt_hook(personal, stdin)
    personal_style = hooks.build_claude_prompt_hook(personal, stdin, personal=True)
    assert "the project's vouch knowledge base" in project_style
    assert "the project's vouch knowledge base" not in personal_style
    assert "personal vouch knowledge base" in personal_style
    assert "may come from other folders" in personal_style


def test_load_store_hint_names_the_personal_kb(
    personal: KBStore, tmp_path: Path, monkeypatch
) -> None:
    """`vouch status` in a KB-less folder must not imply nothing is captured
    there when the personal fallback is on."""
    nowhere = tmp_path / "no-kb"
    nowhere.mkdir()
    monkeypatch.chdir(nowhere)
    r = CliRunner().invoke(cli, ["status"])
    assert r.exit_code == 2
    assert "personal KB" in r.output
    assert "vouch adopt" in r.output


def test_retire_never_archives_a_claim_that_only_landed_pending(
    personal: KBStore, tmp_path: Path
) -> None:
    """BLOCKER regression: archiving the personal copy of a claim the project
    has not accepted yet strands it — reject or expire the proposal and the
    knowledge is live in neither KB, with no unarchive path."""
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    cfg = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    cfg["review"]["auto_approve_on_receipt"] = False
    project.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    report = adopt_mod.adopt(project, personal, match_root=origin, retire=True)
    assert report.claims_durable == []
    assert report.claims_pending
    assert report.retired == []
    for claim_id in report.claims_pending:
        assert personal.get_claim(claim_id).status == ClaimStatus.WORKING


def test_retire_archives_only_the_durable_ones(
    personal: KBStore, tmp_path: Path
) -> None:
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    report = adopt_mod.adopt(project, personal, match_root=origin, retire=True)
    assert report.claims_durable
    assert sorted(report.retired) == sorted(report.claims_durable)


def test_dry_run_honours_a_closed_gate(personal: KBStore, tmp_path: Path) -> None:
    """A preview that promises durable claims a closed gate will leave pending
    is worse than no preview."""
    origin = tmp_path / "projA"
    origin.mkdir()
    _fallback_capture(personal, origin, tmp_path)
    project = KBStore.init(origin)
    cfg = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    cfg["review"]["auto_approve_on_receipt"] = False
    project.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    dry = adopt_mod.adopt(project, personal, match_root=origin, dry_run=True)
    assert dry.claims_durable == []
    assert dry.claims_pending
    real = adopt_mod.adopt(project, personal, match_root=origin)
    assert sorted(real.claims_pending) == sorted(dry.claims_pending)
    assert real.claims_durable == dry.claims_durable


def test_fallback_session_summary_records_its_origin(
    personal: KBStore, tmp_path: Path
) -> None:
    """A rollup filed into the shared personal KB must say which folder it is
    about. The admission gate now auto-rejects an uncited session page, but the
    origin/provenance it carries survives on the rejected proposal rather than
    being left silently behind."""
    from vouch import capture as cap

    origin = tmp_path / "projA"
    origin.mkdir()
    for i in range(3):
        cap.observe(personal, "sum-1", tool="Edit", summary=f"edited f{i}.py",
                    now=float(i))
    result = cap.finalize(personal, "sum-1", cwd=origin, project=origin.name,
                          origin=origin)
    # finalize still returns the id even though the uncited session rollup is
    # auto-rejected at filing by the admission gate.
    assert result["summary_proposal_id"]
    proposal = personal.get_proposal(str(result["summary_proposal_id"]))
    assert proposal.payload["metadata"]["origin_path"] == str(origin)
    assert "personal-fallback" in proposal.payload["tags"]
    # the same origin-bearing proposal is the one the admission gate rejected —
    # explicitly rejected, not silently dropped.
    assert proposal.status is ProposalStatus.REJECTED
    assert proposal.decided_by == "vouch-admission"
    assert proposal.decision_reason.startswith("admission:")
    pending = {p.id for p in personal.list_proposals(ProposalStatus.PENDING)}
    rejected = {p.id for p in personal.list_proposals(ProposalStatus.REJECTED)}
    assert proposal.id not in pending
    assert proposal.id in rejected

    # a rejected page is not adoptable — adopt must not surface it as pending.
    project = KBStore.init(origin)
    report = adopt_mod.adopt(project, personal, match_root=origin)
    assert proposal.id not in report.pages_pending_in_personal


def test_personal_entry_prefers_a_live_row_over_a_stale_one(
    personal: KBStore, tmp_path: Path
) -> None:
    """A registry row left behind by a deleted personal KB must not shadow the
    live one and silently switch fallback off."""
    ghost = tmp_path / "ghost-personal"
    KBStore.init(ghost)
    hub.register_kb(ghost, role="personal", actor="t")
    import shutil

    shutil.rmtree(ghost / ".vouch")

    entry = hub.personal_entry()
    assert entry is not None
    assert Path(entry.path) == personal.root
    fb = hub.personal_fallback_store()
    assert fb is not None and fb.root == personal.root


def test_init_personal_retires_a_row_pointing_elsewhere(
    personal: KBStore, tmp_path: Path, monkeypatch
) -> None:
    """Two personal rows are a routing hazard: capture would follow one KB
    while `hub fallback` flips another."""
    elsewhere = tmp_path / "other-personal"
    monkeypatch.setenv(hub.PERSONAL_KB_ENV, str(elsewhere))
    r = CliRunner().invoke(cli, ["hub", "init-personal", "--fallback"])
    assert r.exit_code == 0, r.output
    rows = hub.personal_entries()
    assert len(rows) == 1
    assert Path(rows[0].path) == elsewhere
    fb = hub.personal_fallback_store()
    assert fb is not None and fb.root == elsewhere


def test_init_personal_reports_an_unwritable_path_cleanly(
    tmp_path: Path, monkeypatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file\n", encoding="utf-8")
    monkeypatch.setenv(hub.PERSONAL_KB_ENV, str(blocker / "personal"))
    r = CliRunner().invoke(cli, ["hub", "init-personal", "--fallback"])
    assert r.exit_code != 0
    assert "could not initialise the personal KB" in r.output
    assert "Traceback" not in r.output


def test_global_install_survives_a_failing_personal_kb(
    tmp_path: Path, monkeypatch
) -> None:
    """The machine-wide wiring is what the command is for: a personal-KB
    failure warns, it does not fail the install."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv(hub.REGISTRY_ENV, str(fake_home / "registry.yaml"))
    blocker = tmp_path / "blocked"
    blocker.write_text("file, not a dir\n", encoding="utf-8")
    monkeypatch.setenv(hub.PERSONAL_KB_ENV, str(blocker / "personal"))
    workdir = tmp_path / "w"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    r = CliRunner().invoke(
        cli, ["install-mcp", "claude-code", "--global", "--personal-fallback"]
    )
    assert r.exit_code == 0, r.output
    assert "could not set up the personal KB" in r.output
    assert (fake_home / ".claude" / "settings.json").is_file()
