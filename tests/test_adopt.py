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
