"""Agent-native provisioning — issue #606."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import agent_provision as ap
from vouch import audit, proposals
from vouch.cli import cli
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path_factory, monkeypatch):
    """Fake HOME / XDG so tests never touch the real machine."""
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / "data"))
    monkeypatch.delenv("VOUCH_KB_PATH", raising=False)
    monkeypatch.delenv("VOUCH_PROJECT_DIR", raising=False)
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.delenv(ap.CREDS_ENV, raising=False)
    monkeypatch.delenv(ap.AGENTS_DATA_ENV, raising=False)
    return fake_home


def _bootstrap(root: Path):
    from vouch.cli import _bootstrap_kb

    return _bootstrap_kb(root)


def test_validate_caller_rejects_bad_names() -> None:
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("")
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("../evil")
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("has space")
    assert ap.validate_caller("ci-bot_1.0") == "ci-bot_1.0"


def test_provision_writes_credential_never_in_public_dict() -> None:
    result = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    assert result.created_kb is True
    assert result.record.caller == "ci-bot"
    assert (Path(result.record.kb_root) / ".vouch" / "config.yaml").is_file()

    public = result.public_dict()
    assert "credential" not in public
    assert public["claim_token"] == result.record.claim_token
    assert public["claim_command"] == f"vouch agents claim {result.record.claim_token}"
    assert public["credential_path"] == str(result.credential_path)
    assert result.record.credential not in json.dumps(public)

    mode = result.credential_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600

    raw = result.credential_path.read_text(encoding="utf-8")
    assert result.record.credential in raw
    assert result.record.claim_token in raw

    cfg = yaml.safe_load(
        (Path(result.record.kb_root) / ".vouch" / "config.yaml").read_text(encoding="utf-8")
    )
    assert cfg["agent"]["caller"] == "ci-bot"
    assert cfg["agent"]["unclaimed"] is True


def test_provision_is_idempotent_before_claim() -> None:
    first = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    second = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    assert second.created_kb is False
    assert second.record.claim_token == first.record.claim_token
    assert second.record.credential == first.record.credential


def test_cli_init_agent_json_omits_credential() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", "--agent", "--agent-caller", "openclaw", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["caller"] == "openclaw"
    assert "credential" not in data
    assert data["claim_command"].startswith("vouch agents claim ")
    # Human-readable path must not leak the secret either.
    assert "export VOUCH_" not in result.output or "credential" not in result.output.lower()
    creds = Path(data["credential_path"]).read_text(encoding="utf-8")
    # The secret lives only on disk.
    stored = yaml.safe_load(creds)["agents"]["openclaw"]["credential"]
    assert stored not in result.output


def test_cli_init_agent_requires_caller() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--agent"])
    assert result.exit_code != 0
    assert "--agent-caller" in result.output


def test_cli_init_agent_caller_requires_agent_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--agent-caller", "x"])
    assert result.exit_code != 0
    assert "--agent" in result.output


def test_claim_adopts_through_the_gate_and_leaves_agent_kb(
    tmp_path: Path,
) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    agent = KBStore(Path(provisioned.record.kb_root))

    # Seed the agent KB with a receipt-backed claim the way capture would.
    body = (
        b"The deploy cadence for this service is every second Tuesday.\n"
        b"Rollbacks use the blue-green switch.\n"
    )
    src = agent.put_source(body, title="ops-note", source_type="file")
    quote = "The deploy cadence for this service is every second Tuesday."
    filed = proposals.propose_quoted_claim(
        agent,
        text=quote,
        source_id=src.id,
        quote=quote,
        proposed_by="ci-bot",
    )
    assert filed is not None
    durable = proposals.resolve_pending_receipt_claim(
        agent, filed.proposal, actor="ci-bot", reason="self-approve under trusted-agent"
    )
    assert durable is not None

    project_root = tmp_path / "proj"
    project_root.mkdir()
    project = KBStore.init(project_root)

    claimed = ap.claim(provisioned.record.claim_token, project, actor="human")
    assert claimed.already_claimed is False
    assert claimed.record.claimed_project_kb_id == project.identity()[0]
    assert durable.id in claimed.adopt.claims_durable
    # Agent KB still has the claim — artifacts untouched.
    assert agent.get_claim(durable.id).text == quote
    # Project received it through the gate.
    assert project.get_claim(durable.id).text == quote
    assert "adopted" in project.get_claim(durable.id).tags

    cfg = yaml.safe_load(agent.config_path.read_text(encoding="utf-8"))
    assert cfg["agent"]["unclaimed"] is False

    events = [e for e in audit.read_events(project.kb_dir) if e.event == "agent.claim"]
    assert events and events[0].data["caller"] == "ci-bot"


def test_claim_is_idempotent_on_same_project(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    project = KBStore.init(tmp_path / "proj")
    first = ap.claim(provisioned.record.claim_token, project, actor="human")
    second = ap.claim(provisioned.record.claim_token, project, actor="human")
    assert first.already_claimed is False
    assert second.already_claimed is True
    assert second.record.claimed_project_kb_id == project.identity()[0]


def test_claim_rejects_other_project(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    a = KBStore.init(tmp_path / "a")
    b = KBStore.init(tmp_path / "b")
    ap.claim(provisioned.record.claim_token, a, actor="human")
    with pytest.raises(ap.AgentProvisionError, match="already claimed"):
        ap.claim(provisioned.record.claim_token, b, actor="human")


def test_claim_unknown_token(tmp_path: Path) -> None:
    project = KBStore.init(tmp_path / "proj")
    with pytest.raises(ap.AgentProvisionError, match="unknown claim token"):
        ap.claim("not-a-real-token", project, actor="human")


def test_cli_agents_claim_end_to_end(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    init = runner.invoke(cli, ["init", "--agent", "--agent-caller", "hermes", "--json"])
    assert init.exit_code == 0, init.output
    data = json.loads(init.output)
    token = data["claim_token"]

    # Put a claim in the agent KB via the library (CLI propose needs more setup).
    agent = KBStore(Path(data["kb_root"]))
    body = b"Hermes remembers that the staging refresh is nightly at 02:00 UTC.\n"
    src = agent.put_source(body, title="note", source_type="file")
    quote = "the staging refresh is nightly at 02:00 UTC"
    assert quote.encode() in body
    filed = proposals.propose_quoted_claim(
        agent,
        text=f"Remember: {quote}.",
        source_id=src.id,
        quote=quote,
        proposed_by="hermes",
    )
    assert filed is not None
    proposals.resolve_pending_receipt_claim(
        agent, filed.proposal, actor="hermes", reason="trusted-agent"
    )

    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    runner.invoke(cli, ["init"])
    claim = runner.invoke(cli, ["agents", "claim", token, "--json"])
    assert claim.exit_code == 0, claim.output
    report = json.loads(claim.output)
    assert report["caller"] == "hermes"
    assert report["already_claimed"] is False
    assert report["adopt"]["claims_durable"] or report["adopt"]["claims_pending"]


def test_caller_from_store_used_when_vouch_agent_unset(
    monkeypatch,
) -> None:
    result = ap.provision("stamped-bot", bootstrap=_bootstrap, actor="human")
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_KB_PATH", str(result.record.kb_dir))
    # discover_root via VOUCH_KB_PATH
    from vouch.cli import _whoami

    assert _whoami() == "stamped-bot"


def test_server_agent_falls_back_to_stamped_caller(monkeypatch) -> None:
    result = ap.provision("mcp-bot", bootstrap=_bootstrap, actor="human")
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_KB_PATH", str(result.record.kb_dir))
    from vouch import server

    # Reset any cached store
    monkeypatch.setattr(server, "_store", lambda: KBStore(Path(result.record.kb_root)))
    assert server._agent() == "mcp-bot"


def test_public_dict_never_contains_credential_after_claim(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    project = KBStore.init(tmp_path / "proj")
    claimed = ap.claim(provisioned.record.claim_token, project, actor="human")
    blob = json.dumps(claimed.public_dict())
    assert provisioned.record.credential not in blob
