"""Agent-native provisioning — issue #606."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import agent_provision as ap
from vouch import agents, audit, proposals, trust
from vouch.cli import cli
from vouch.models import Claim
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
    monkeypatch.delenv("VOUCH_USER", raising=False)
    monkeypatch.delenv(ap.CREDS_ENV, raising=False)
    monkeypatch.delenv(ap.AGENTS_DATA_ENV, raising=False)
    return fake_home


def _bootstrap(root: Path):
    from vouch.cli import _bootstrap_kb

    return _bootstrap_kb(root)


def _seed_receipt_claim(store: KBStore, *, caller: str = "ci-bot") -> Claim:
    body = (
        b"The deploy cadence for this service is every second Tuesday.\n"
        b"Rollbacks use the blue-green switch.\n"
    )
    src = store.put_source(body, title="ops-note", source_type="file")
    quote = "The deploy cadence for this service is every second Tuesday."
    filed = proposals.propose_quoted_claim(
        store,
        text=quote,
        source_id=src.id,
        quote=quote,
        proposed_by=caller,
    )
    assert filed is not None
    durable = proposals.resolve_pending_receipt_claim(
        store, filed.proposal, actor=caller, reason="trusted-agent"
    )
    assert durable is not None
    return durable


def test_validate_caller_rejects_bad_names() -> None:
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("")
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("../evil")
    with pytest.raises(ap.AgentProvisionError):
        ap.validate_caller("has space")
    assert ap.validate_caller("ci-bot_1.0") == "ci-bot_1.0"


def test_credentials_and_data_path_overrides(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "creds.yaml"
    data = tmp_path / "agents-data"
    monkeypatch.setenv(ap.CREDS_ENV, str(creds))
    monkeypatch.setenv(ap.AGENTS_DATA_ENV, str(data))
    assert ap.credentials_path() == creds
    assert ap.agents_data_root() == data


def test_agents_data_root_falls_back_to_home(monkeypatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv(ap.AGENTS_DATA_ENV, raising=False)
    root = ap.agents_data_root()
    assert root.as_posix().endswith(".local/share/vouch/agents")


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


def test_provision_rejects_missing_kb_on_rerun() -> None:
    first = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    import shutil

    shutil.rmtree(Path(first.record.kb_root) / ".vouch")
    with pytest.raises(ap.AgentProvisionError, match="KB is missing"):
        ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")


def test_provision_rejects_already_claimed(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    project = KBStore.init(tmp_path / "proj")
    ap.claim(provisioned.record.claim_token, project, actor="human")
    with pytest.raises(ap.AgentProvisionError, match="already claimed"):
        ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")


def test_provision_with_explicit_path(tmp_path: Path) -> None:
    root = tmp_path / "custom-agent"
    result = ap.provision(
        "path-bot", bootstrap=_bootstrap, path=root, actor="human"
    )
    assert Path(result.record.kb_root) == root.resolve()
    assert ap.agent_kb_root("path-bot", path=root) == root.resolve()


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
    assert "export VOUCH_" not in result.output or "credential" not in result.output.lower()
    creds = Path(data["credential_path"]).read_text(encoding="utf-8")
    stored = yaml.safe_load(creds)["agents"]["openclaw"]["credential"]
    assert stored not in result.output


def test_cli_init_agent_human_text() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--agent", "--agent-caller", "text-bot"])
    assert result.exit_code == 0, result.output
    assert "Initialised agent KB" in result.output or "Reused agent KB" in result.output
    assert "Credential written" in result.output
    assert "vouch agents claim " in result.output
    assert "export VOUCH_AGENT=text-bot" in result.output


def test_cli_init_human_json(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "kb_dir" in data
    assert data["starter_created"] is True


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


def test_claim_adopts_through_the_gate_and_leaves_agent_kb(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    agent = KBStore(Path(provisioned.record.kb_root))
    durable = _seed_receipt_claim(agent)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    project = KBStore.init(project_root)

    claimed = ap.claim(provisioned.record.claim_token, project, actor="human")
    assert claimed.already_claimed is False
    assert claimed.record.claimed_project_kb_id == project.identity()[0]
    assert durable.id in claimed.adopt.claims_durable
    assert agent.get_claim(durable.id).text == durable.text
    assert project.get_claim(durable.id).text == durable.text
    assert "adopted" in project.get_claim(durable.id).tags

    cfg = yaml.safe_load(agent.config_path.read_text(encoding="utf-8"))
    assert cfg["agent"]["unclaimed"] is False

    events = [e for e in audit.read_events(project.kb_dir) if e.event == "agent.claim"]
    assert events and events[0].data["caller"] == "ci-bot"


def test_claim_dry_run_writes_nothing(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    agent = KBStore(Path(provisioned.record.kb_root))
    _seed_receipt_claim(agent)
    project = KBStore.init(tmp_path / "proj")
    before = ap.find_by_caller("ci-bot")
    assert before is not None and before.claimed_at is None
    result = ap.claim(
        provisioned.record.claim_token, project, actor="human", dry_run=True
    )
    assert result.already_claimed is False
    assert result.adopt.dry_run is True
    assert result.adopt.claims_durable
    after = ap.find_by_caller("ci-bot")
    assert after is not None and after.claimed_at is None


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


def test_claim_empty_token(tmp_path: Path) -> None:
    project = KBStore.init(tmp_path / "proj")
    with pytest.raises(ap.AgentProvisionError, match="empty"):
        ap.claim("   ", project, actor="human")


def test_claim_refuses_agent_own_kb() -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    agent = KBStore(Path(provisioned.record.kb_root))
    with pytest.raises(ap.AgentProvisionError, match="project KB"):
        ap.claim(provisioned.record.claim_token, agent, actor="human")


def test_claim_refuses_missing_agent_kb(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    import shutil

    shutil.rmtree(Path(provisioned.record.kb_root) / ".vouch")
    project = KBStore.init(tmp_path / "proj")
    with pytest.raises(ap.AgentProvisionError, match="missing"):
        ap.claim(provisioned.record.claim_token, project, actor="human")


def test_claim_mints_project_identity_when_absent(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    project = KBStore.init(tmp_path / "proj")
    cfg = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    cfg.pop("kb", None)
    project.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert project.identity() is None
    claimed = ap.claim(provisioned.record.claim_token, project, actor="human")
    assert claimed.record.claimed_project_kb_id == project.identity()[0]


def test_cli_agents_claim_end_to_end(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    init = runner.invoke(cli, ["init", "--agent", "--agent-caller", "hermes", "--json"])
    assert init.exit_code == 0, init.output
    data = json.loads(init.output)
    token = data["claim_token"]

    agent = KBStore(Path(data["kb_root"]))
    _seed_receipt_claim(agent, caller="hermes")

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

    store = KBStore(project)
    registered = agents.find(store, "hermes")
    assert registered is not None
    assert registered.subject == trust.auth_subject_for_token(
        yaml.safe_load(Path(data["credential_path"]).read_text(encoding="utf-8"))
        ["agents"]["hermes"]["credential"]
    )


def test_cli_agents_claim_human_text(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    init = runner.invoke(cli, ["init", "--agent", "--agent-caller", "text-claim", "--json"])
    data = json.loads(init.output)
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    runner.invoke(cli, ["init"])
    claim = runner.invoke(cli, ["agents", "claim", data["claim_token"]])
    assert claim.exit_code == 0, claim.output
    assert "Claimed agent 'text-claim'" in claim.output


def test_cli_agents_claim_dry_run_text(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    init = runner.invoke(cli, ["init", "--agent", "--agent-caller", "dry", "--json"])
    data = json.loads(init.output)
    project = tmp_path / "app"
    project.mkdir()
    monkeypatch.chdir(project)
    runner.invoke(cli, ["init"])
    claim = runner.invoke(cli, ["agents", "claim", data["claim_token"], "--dry-run"])
    assert claim.exit_code == 0, claim.output
    assert "Would claim" in claim.output


def test_caller_from_store_used_when_vouch_agent_unset(monkeypatch) -> None:
    result = ap.provision("stamped-bot", bootstrap=_bootstrap, actor="human")
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    monkeypatch.setenv("VOUCH_KB_PATH", str(result.record.kb_dir))
    from vouch.cli import _whoami

    assert _whoami() == "stamped-bot"


def test_whoami_prefers_vouch_agent_env(monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "env-bot")
    from vouch.cli import _whoami

    assert _whoami() == "env-bot"


def test_caller_from_store_error_paths(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "plain")
    store.config_path.write_text("[]\n", encoding="utf-8")
    assert ap.caller_from_store(store) is None
    store.config_path.unlink()
    assert ap.caller_from_store(store) is None


def test_stamp_agent_config_recovers_non_mapping(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "plain")
    # Truthy non-mapping so `yaml.safe_load(...) or {}` does not short-circuit.
    store.config_path.write_text("[1]\n", encoding="utf-8")
    ap.stamp_agent_config(store, caller="bot", unclaimed=True)
    loaded = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    assert loaded["agent"]["caller"] == "bot"


def test_list_records_rejects_non_dict_agents(monkeypatch) -> None:
    monkeypatch.setattr(
        ap, "_load_raw", lambda: {"version": 1, "agents": ["not", "a", "map"]}
    )
    assert ap.list_records() == []


def test_server_agent_falls_back_to_stamped_caller(monkeypatch) -> None:
    result = ap.provision("mcp-bot", bootstrap=_bootstrap, actor="human")
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    from vouch import server

    monkeypatch.setattr(server, "_store", lambda: KBStore(Path(result.record.kb_root)))
    with trust.trust_context(trust.MCP_STDIO):
        assert server._agent() == "mcp-bot"


def test_server_agent_env_and_unknown(monkeypatch, tmp_path: Path) -> None:
    from vouch import server

    monkeypatch.setenv("VOUCH_AGENT", "env-mcp")
    with trust.trust_context(trust.MCP_STDIO):
        assert server._agent() == "env-mcp"

    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    plain = KBStore.init(tmp_path / "plain-mcp")
    monkeypatch.setattr(server, "_store", lambda: plain)
    with trust.trust_context(trust.MCP_STDIO):
        assert server._agent() == "unknown-agent"


def test_jsonl_agent_falls_back_to_stamped_caller(monkeypatch) -> None:
    result = ap.provision("jsonl-bot", bootstrap=_bootstrap, actor="human")
    monkeypatch.delenv("VOUCH_AGENT", raising=False)
    from vouch import jsonl_server

    monkeypatch.setattr(
        jsonl_server, "_store", lambda: KBStore(Path(result.record.kb_root))
    )
    token = jsonl_server._actor.set(None)
    try:
        with trust.trust_context(trust.JSONL_HTTP):
            assert jsonl_server._agent() == "jsonl-bot"
    finally:
        jsonl_server._actor.reset(token)


def test_jsonl_agent_header_env_and_unknown(monkeypatch, tmp_path: Path) -> None:
    from vouch import jsonl_server

    token = jsonl_server._actor.set("header-bot")
    try:
        with trust.trust_context(trust.JSONL_HTTP):
            assert jsonl_server._agent() == "header-bot"
    finally:
        jsonl_server._actor.reset(token)

    token = jsonl_server._actor.set(None)
    try:
        monkeypatch.setenv("VOUCH_AGENT", "env-jsonl")
        with trust.trust_context(trust.JSONL_HTTP):
            assert jsonl_server._agent() == "env-jsonl"
        monkeypatch.delenv("VOUCH_AGENT", raising=False)
        plain = KBStore.init(tmp_path / "plain-jsonl")
        monkeypatch.setattr(jsonl_server, "_store", lambda: plain)
        with trust.trust_context(trust.JSONL_HTTP):
            assert jsonl_server._agent() == "unknown-agent"
    finally:
        jsonl_server._actor.reset(token)


def test_public_dict_never_contains_credential_after_claim(tmp_path: Path) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    project = KBStore.init(tmp_path / "proj")
    claimed = ap.claim(provisioned.record.claim_token, project, actor="human")
    blob = json.dumps(claimed.public_dict())
    assert provisioned.record.credential not in blob


def test_load_raw_handles_corrupt_and_non_mapping(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "creds.yaml"
    monkeypatch.setenv(ap.CREDS_ENV, str(creds))
    creds.write_text("{not yaml", encoding="utf-8")
    with pytest.raises(ap.AgentProvisionError, match="cannot read"):
        ap.list_records()

    creds.write_text("[]\n", encoding="utf-8")
    assert ap.list_records() == []

    creds.write_text("version: 1\nagents: []\n", encoding="utf-8")
    assert ap.list_records() == []

    creds.write_text(
        "version: 1\nagents:\n  1: {kb_root: x, credential: y, claim_token: z}\n"
        "  bad: not-a-map\n"
        "  incomplete: {kb_root: x}\n",
        encoding="utf-8",
    )
    # non-string keys skipped; bad/incomplete parse to None
    assert ap.list_records() == []


def test_save_raw_cleans_up_temp_on_failure(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "creds.yaml"
    monkeypatch.setenv(ap.CREDS_ENV, str(creds))
    # Seed a valid provision first so _save_raw is exercised, then break replace.
    ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ap.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        ap._save_raw({"version": 1, "agents": {}})


def test_provision_repairs_non_dict_agents_bucket(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "creds.yaml"
    monkeypatch.setenv(ap.CREDS_ENV, str(creds))
    creds.parent.mkdir(parents=True, exist_ok=True)
    # Non-empty list so setdefault returns it and the isinstance repair runs.
    creds.write_text("version: 1\nagents: [x]\n", encoding="utf-8")
    # _load_raw normalises list → {}; force a truthy non-dict through setdefault.
    real_load = ap._load_raw

    def load_with_list_agents():
        data = real_load()
        data["agents"] = ["x"]
        return data

    monkeypatch.setattr(ap, "_load_raw", load_with_list_agents)
    result = ap.provision("repair-bot", bootstrap=_bootstrap, actor="human")
    assert result.record.caller == "repair-bot"


def test_claim_repairs_non_dict_agents_bucket(tmp_path: Path, monkeypatch) -> None:
    provisioned = ap.provision("ci-bot", bootstrap=_bootstrap, actor="human")
    # Corrupt agents to a list but keep the record reachable via find_by_claim_token
    # by writing the record under a dict first, then... actually find uses list_records
    # which returns [] for list agents. So claim unknown. Instead corrupt AFTER find
    # by patching _load_raw mid-claim — simpler: rewrite file between find and save
    # by making agents a list that still... won't work with find.

    # Corrupt after locating: mutate file before claim's final _save_raw by
    # writing agents as list containing nothing useful, then force find via
    # monkeypatch.
    project = KBStore.init(tmp_path / "proj")
    record = provisioned.record

    def load_then_corrupt():
        data = {"version": 1, "agents": []}
        return data

    monkeypatch.setattr(ap, "_load_raw", load_then_corrupt)
    monkeypatch.setattr(ap, "find_by_claim_token", lambda _t: record)
    claimed = ap.claim(record.claim_token, project, actor="human")
    assert claimed.record.claimed_at is not None
