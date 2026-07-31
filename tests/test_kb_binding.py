"""Dedicated agent KBs and credentials bound to one KB — issue #609."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import agents, audit, hub, kb_binding, trust
from vouch.cli import cli
from vouch.kb_binding import Binding, BindingError
from vouch.storage import KBStore


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path_factory, monkeypatch):
    """Fake $HOME plus registry/binding paths so tests never touch the machine.

    Mirrors test_hub's isolator, and adds the binding file: an authorization
    decision keyed on a real developer's ~/.config would be a nasty surprise.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv(hub.REGISTRY_ENV, str(fake_home / "registry.yaml"))
    monkeypatch.setenv(kb_binding.BINDINGS_ENV, str(fake_home / "credentials.yaml"))
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / "data"))
    for var in ("VOUCH_KB_PATH", "VOUCH_PROJECT_DIR", hub.PERSONAL_KB_ENV,
                hub.AGENT_KBS_ENV, "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    return fake_home


@pytest.fixture
def project(tmp_path: Path) -> KBStore:
    """A human's project KB, registered the ordinary way."""
    root = tmp_path / "proj"
    store = KBStore.init(root)
    hub.register_kb(root, actor="human")
    return store


def _create(*args: str):
    return CliRunner().invoke(cli, ["kb", "create", *args])


def _bound_subject() -> str:
    return kb_binding.load_bindings()[0].subject


# --- the binding file -----------------------------------------------------


def test_a_bound_subject_reaches_its_own_kb_and_no_other(
    project: KBStore, tmp_path: Path
) -> None:
    """The whole point: one credential, one KB."""
    agent_kb = KBStore.init(tmp_path / "agent")
    kb_binding.bind(subject="abc123", kb_id=agent_kb.identity()[0], agent="ci")  # type: ignore[index]

    assert kb_binding.store_allows(agent_kb, "abc123") is True
    assert kb_binding.store_allows(project, "abc123") is False


def test_an_unbound_subject_is_unaffected(project: KBStore) -> None:
    """Binding is opt-in: a token that predates it keeps working everywhere."""
    assert kb_binding.allows("never-bound", "any-kb-id") is True
    assert kb_binding.store_allows(project, "never-bound") is True


def test_a_bound_subject_is_refused_by_a_kb_with_no_identity(
    tmp_path: Path,
) -> None:
    """Stripping `kb.id` must not become an escape hatch from the binding."""
    anonymous = KBStore.init(tmp_path / "anon")
    kb_binding.bind(subject="abc123", kb_id="somewhere-else")
    config = yaml.safe_load(anonymous.config_path.read_text(encoding="utf-8"))
    config.pop("kb")
    anonymous.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert anonymous.identity() is None
    assert kb_binding.store_allows(anonymous, "abc123") is False
    # …while an unbound subject still authenticates against it as before.
    assert kb_binding.store_allows(anonymous, "unbound") is True


def test_binding_file_is_owner_readable_and_never_holds_the_secret() -> None:
    token = kb_binding.issue_token()
    subject = trust.auth_subject_for_token(token)
    kb_binding.bind(subject=subject, kb_id="kb-1", kb_name="ci", agent="bot")

    path = kb_binding.bindings_path()
    raw = path.read_text(encoding="utf-8")
    assert subject in raw
    assert token not in raw
    assert path.stat().st_mode & 0o777 == 0o600


def test_bind_refuses_to_move_a_live_credential() -> None:
    kb_binding.bind(subject="s1", kb_id="kb-a")
    with pytest.raises(BindingError, match="already bound to kb kb-a"):
        kb_binding.bind(subject="s1", kb_id="kb-b")
    # Re-binding to the same KB is a harmless refresh, not a move.
    assert kb_binding.bind(subject="s1", kb_id="kb-a").kb_id == "kb-a"
    assert len(kb_binding.load_bindings()) == 1


def test_bind_needs_a_subject_and_a_kb_id() -> None:
    with pytest.raises(BindingError, match="auth subject"):
        kb_binding.bind(subject="  ", kb_id="kb-a")
    with pytest.raises(BindingError, match="target kb id"):
        kb_binding.bind(subject="s1", kb_id="")


def test_unbind_removes_one_row_and_is_quiet_about_the_rest() -> None:
    kb_binding.bind(subject="s1", kb_id="kb-a")
    kb_binding.bind(subject="s2", kb_id="kb-a")

    assert kb_binding.unbind("s1") is not None
    assert kb_binding.unbind("s1") is None
    assert [b.subject for b in kb_binding.load_bindings()] == ["s2"]
    assert [b.subject for b in kb_binding.bindings_for_kb("kb-a")] == ["s2"]
    assert kb_binding.bindings_for_kb("kb-z") == []


def test_binding_for_finds_by_subject() -> None:
    kb_binding.bind(subject="s1", kb_id="kb-a")
    assert kb_binding.binding_for(" s1 ") is not None
    assert kb_binding.binding_for("s9") is None


@pytest.mark.parametrize(
    "body",
    [
        "",  # not a mapping
        "bindings: not-a-list",
        "bindings: [null, 3, {}, {subject: s}, {kb_id: k}, {subject: '', kb_id: k}]",
        "{{{ not yaml",
    ],
)
def test_an_unreadable_binding_file_denies_nobody(body: str) -> None:
    """Fail-open on corruption: this file must never lock a fleet out."""
    kb_binding.bindings_path().parent.mkdir(parents=True, exist_ok=True)
    kb_binding.bindings_path().write_text(body, encoding="utf-8")
    assert kb_binding.load_bindings() == []
    assert kb_binding.allows("anyone", "any-kb") is True


def test_a_missing_binding_file_denies_nobody() -> None:
    assert not kb_binding.bindings_path().exists()
    assert kb_binding.load_bindings() == []


def test_a_duplicated_subject_resolves_to_one_answer() -> None:
    """Two answers to "which KB may this reach" is the ambiguity to remove."""
    kb_binding.bindings_path().parent.mkdir(parents=True, exist_ok=True)
    kb_binding.bindings_path().write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "bindings": [
                    {"subject": "s1", "kb_id": "kb-a"},
                    {"subject": "s1", "kb_id": "kb-b"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert [b.kb_id for b in kb_binding.load_bindings()] == ["kb-a"]


def test_a_failed_write_leaves_no_stray_temp_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-written credential file is worse than no write at all."""
    path = kb_binding.bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        kb_binding.yaml, "safe_dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        kb_binding.save_bindings([Binding(subject="s", kb_id="k")])
    assert list(path.parent.iterdir()) == []


def test_optional_binding_fields_are_omitted_when_empty() -> None:
    assert Binding(subject="s", kb_id="k").to_dict() == {"subject": "s", "kb_id": "k"}
    full = Binding(
        subject="s", kb_id="k", kb_name="n", agent="a", bound_at="t"
    ).to_dict()
    assert full["kb_name"] == "n" and full["agent"] == "a" and full["bound_at"] == "t"


def test_bindings_path_prefers_env_then_xdg_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(kb_binding.BINDINGS_ENV, str(tmp_path / "forced.yaml"))
    assert kb_binding.bindings_path() == tmp_path / "forced.yaml"
    monkeypatch.delenv(kb_binding.BINDINGS_ENV)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert kb_binding.bindings_path() == tmp_path / "xdg" / "vouch" / "credentials.yaml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert kb_binding.bindings_path() == Path.home() / ".config" / "vouch" / "credentials.yaml"


def test_issued_tokens_are_unguessable_and_distinct() -> None:
    tokens = {kb_binding.issue_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 40 for t in tokens)


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("pr-triager", "VOUCH_TOKEN_PR_TRIAGER"),
        ("incident.summariser", "VOUCH_TOKEN_INCIDENT_SUMMARISER"),
        ("---", "VOUCH_TOKEN"),
    ],
)
def test_token_env_var_is_shell_safe(agent: str, expected: str) -> None:
    assert kb_binding.token_env_var(agent) == expected


# --- the transport chokepoint ---------------------------------------------


def test_the_gate_refuses_a_bound_token_against_the_project_kb(
    project: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaked-CI-secret case: right token, wrong working directory."""
    agent_kb = KBStore.init(tmp_path / "agent")
    token = kb_binding.issue_token()
    subject = trust.auth_subject_for_token(token)
    kb_binding.bind(subject=subject, kb_id=agent_kb.identity()[0], agent="ci")  # type: ignore[index]

    monkeypatch.chdir(agent_kb.root)
    assert agents.subject_is_active(subject) is True
    monkeypatch.chdir(project.root)
    assert agents.subject_is_active(subject) is False


def test_the_gate_still_denies_a_revoked_agent_in_its_own_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding is an extra gate, not a replacement for revocation."""
    agent_kb = KBStore.init(tmp_path / "agent")
    subject = trust.auth_subject_for_token(kb_binding.issue_token())
    agents.register(agent_kb, subject=subject, name="ci", actor="human")
    kb_binding.bind(subject=subject, kb_id=agent_kb.identity()[0])  # type: ignore[index]
    agents.set_status(agent_kb, "ci", agents.AgentStatus.REVOKED, actor="human")

    monkeypatch.chdir(agent_kb.root)
    assert agents.subject_is_active(subject) is False


def test_the_gate_is_unchanged_for_deployments_with_no_bindings(
    project: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project.root)
    assert agents.subject_is_active(trust.auth_subject_for_token("legacy")) is True


# --- the registry gains an owner ------------------------------------------


def test_ownership_round_trips_and_older_rows_read_as_human_owned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kb"
    KBStore.init(root)
    entry = hub.register_kb(root, actor="human", owner="alice-example", agent="ci-bot")
    assert entry.machine_owned is True

    [reloaded] = hub.load_registry()
    assert (reloaded.owner, reloaded.agent) == ("alice-example", "ci-bot")
    assert hub.agent_entries() == [reloaded]

    plain = hub.RegistryEntry(
        kb_id="k", name="n", role="project", path="/p", added_at="t"
    )
    assert plain.machine_owned is False
    assert "owner" not in hub._row(plain) and "agent" not in hub._row(plain)


def test_ownership_survives_a_plain_re_register(tmp_path: Path) -> None:
    """`vouch hub register` must not launder an agent KB into a human one."""
    root = tmp_path / "kb"
    KBStore.init(root)
    first = hub.register_kb(root, actor="human", owner="alice-example", agent="ci-bot")
    again = hub.register_kb(root, actor="human")

    assert (again.owner, again.agent) == ("alice-example", "ci-bot")
    assert again.added_at == first.added_at


def test_ambient_capture_refuses_to_land_in_an_agent_kb(tmp_path: Path) -> None:
    """An agent KB caught by upward discovery is the personal-KB hazard."""
    root = tmp_path / "outer"
    KBStore.init(root)
    hub.register_kb(root, actor="human", owner="alice-example", agent="ci-bot")
    nested = root / "project"
    nested.mkdir()

    res = hub.resolve(nested)
    assert res.root == root.resolve()
    assert res.guard is not None and "provisioned for agent 'ci-bot'" in res.guard
    assert hub.resolve_for_capture(nested) is None
    # From the KB's own root it is not ambient — it is the deliberate target.
    assert hub.resolve(root).guard is None


def test_agent_kb_root_prefers_env_then_xdg_then_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(hub.AGENT_KBS_ENV, str(tmp_path / "forced"))
    assert hub.agent_kb_root("ci") == tmp_path / "forced" / "ci"
    monkeypatch.delenv(hub.AGENT_KBS_ENV)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert hub.agent_kb_root("ci") == tmp_path / "xdg" / "vouch" / "kbs" / "ci"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert hub.agent_kb_root("ci") == Path.home() / ".local/share/vouch/kbs/ci"


def test_agent_kb_root_sanitises_the_name_and_refuses_an_empty_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(hub.AGENT_KBS_ENV, str(tmp_path))
    assert hub.agent_kb_root("../../etc/passwd") == tmp_path / "etc-passwd"
    with pytest.raises(ValueError, match="cannot derive a directory name"):
        hub.agent_kb_root("///")


def test_agent_kb_root_is_none_without_a_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def _no_home(cls):
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    assert hub.agent_kb_root("ci") is None


# --- cli ------------------------------------------------------------------


def test_cli_create_provisions_registers_and_issues_one_credential(
    project: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project.root)
    res = _create("ci-triage", "--for-agent", "pr-triager")
    assert res.exit_code == 0, res.output

    entry = hub.agent_entries()[0]
    assert (entry.name, entry.agent) == ("ci-triage", "pr-triager")
    agent_kb = KBStore(Path(entry.path))

    # The token is echoed exactly once and never written to disk.
    env_var = kb_binding.token_env_var("pr-triager")
    token = next(
        line.split("=", 1)[1].strip()
        for line in res.output.splitlines()
        if line.strip().startswith(env_var)
    )
    assert trust.auth_subject_for_token(token) == _bound_subject()
    assert token not in kb_binding.bindings_path().read_text(encoding="utf-8")
    assert token not in (agent_kb.kb_dir / "agents.yaml").read_text(encoding="utf-8")

    # It authenticates against its own KB and not against the project.
    assert kb_binding.store_allows(agent_kb, _bound_subject()) is True
    assert kb_binding.store_allows(project, _bound_subject()) is False

    # The agent is named in its own KB, and the accept-list points at the
    # env var rather than carrying the secret.
    assert [a.name for a in agents.load_registry(agent_kb)] == ["pr-triager"]
    config = yaml.safe_load(agent_kb.config_path.read_text(encoding="utf-8"))
    assert config["serve"]["bearer_tokens"] == [f"env:{env_var}"]
    assert any(
        e.event == "agent.bind"
        for e in audit.read_events(agent_kb.kb_dir)
    )


def test_cli_create_without_an_agent_is_just_a_registered_kb(tmp_path: Path) -> None:
    res = _create("scratch", "--path", str(tmp_path / "scratch"))
    assert res.exit_code == 0, res.output
    assert "credential" not in res.output
    assert kb_binding.load_bindings() == []
    [entry] = hub.load_registry()
    assert entry.machine_owned is False and entry.owner


def test_cli_create_refuses_to_clobber_an_existing_kb(tmp_path: Path) -> None:
    dest = tmp_path / "taken"
    KBStore.init(dest)
    res = _create("taken", "--path", str(dest))
    assert res.exit_code != 0
    assert "already exists" in res.output
    assert "Traceback" not in res.output


def test_cli_create_reports_a_homeless_machine_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    def _no_home(cls):
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    res = _create("ci")
    assert res.exit_code != 0
    assert hub.AGENT_KBS_ENV in res.output
    assert "Traceback" not in res.output


def test_cli_create_rejects_a_name_it_cannot_turn_into_a_directory() -> None:
    res = _create("///")
    assert res.exit_code != 0
    assert "cannot derive a directory name" in res.output
    assert "Traceback" not in res.output


def test_cli_create_leaves_nothing_behind_when_init_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A half-built KB is one a rerun would mistake for a finished one."""
    def _boom(root, **kwargs):
        (root / ".vouch").mkdir(parents=True, exist_ok=True)
        raise OSError("read-only filesystem")

    monkeypatch.setattr("vouch.cli._bootstrap_kb", _boom)
    dest = tmp_path / "doomed"
    res = _create("doomed", "--path", str(dest))

    assert res.exit_code != 0
    assert "could not initialise the KB" in res.output
    assert "Traceback" not in res.output
    assert not (dest / ".vouch").exists()


def test_cli_issue_rotates_a_credential_for_an_existing_kb() -> None:
    assert _create("ci-triage", "--for-agent", "pr-triager").exit_code == 0
    first = _bound_subject()

    res = CliRunner().invoke(
        cli, ["kb", "issue", "ci-triage", "--for-agent", "pr-triager-2"]
    )
    assert res.exit_code == 0, res.output
    subjects = {b.subject for b in kb_binding.load_bindings()}
    assert first in subjects and len(subjects) == 2
    # Both are bound to the same KB, and the accept-list already names the
    # first agent's var — so only the second one needs a nudge.
    assert len({b.kb_id for b in kb_binding.load_bindings()}) == 1
    assert "add `env:VOUCH_TOKEN_PR_TRIAGER_2`" in res.output


def test_cli_issue_says_nothing_when_the_accept_list_already_names_the_var() -> None:
    assert _create("ci-triage", "--for-agent", "bot").exit_code == 0
    res = CliRunner().invoke(cli, ["kb", "issue", "ci-triage", "--for-agent", "bot2"])
    assert res.exit_code == 0, res.output
    assert "add `env:" in res.output

    entry = hub.agent_entries()[0]
    store = KBStore(Path(entry.path))
    config = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    config["serve"]["bearer_tokens"].append("env:VOUCH_TOKEN_BOT3")
    store.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    res = CliRunner().invoke(cli, ["kb", "issue", "ci-triage", "--for-agent", "bot3"])
    assert res.exit_code == 0, res.output
    assert "add `env:" not in res.output


def test_cli_issue_errors_cleanly_on_an_unknown_or_moved_kb(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["kb", "issue", "nope", "--for-agent", "bot"])
    assert res.exit_code != 0
    assert "no agent KB called 'nope'" in res.output

    assert _create("gone", "--for-agent", "bot", "--path", str(tmp_path / "gone")).exit_code == 0
    import shutil as _shutil

    _shutil.rmtree(tmp_path / "gone")
    res = runner.invoke(cli, ["kb", "issue", "gone", "--for-agent", "bot"])
    assert res.exit_code != 0
    assert "there is no .vouch/ there" in res.output
    assert "Traceback" not in res.output


def test_cli_issue_refuses_a_duplicate_agent_name() -> None:
    assert _create("ci-triage", "--for-agent", "bot").exit_code == 0
    res = CliRunner().invoke(cli, ["kb", "issue", "ci-triage", "--for-agent", "bot"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output


def test_cli_list_reports_agent_kbs_only() -> None:
    runner = CliRunner()
    empty = runner.invoke(cli, ["kb", "list"])
    assert empty.exit_code == 0 and "no agent KBs" in empty.output

    assert _create("ci-triage", "--for-agent", "pr-triager").exit_code == 0
    table = runner.invoke(cli, ["kb", "list"])
    assert table.exit_code == 0, table.output
    assert "ci-triage" in table.output and "agent=pr-triager" in table.output
    assert "credentials=1" in table.output

    res = runner.invoke(cli, ["kb", "list", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    [row] = payload["kbs"]
    assert row["agent"] == "pr-triager"
    assert row["credentials"] == 1
    assert row["owner"]


def test_cli_list_shows_a_dash_for_an_unrecorded_owner(tmp_path: Path) -> None:
    root = tmp_path / "kb"
    KBStore.init(root)
    hub.register_kb(root, actor="human", agent="ci-bot")
    res = CliRunner().invoke(cli, ["kb", "list"])
    assert res.exit_code == 0, res.output
    assert "owner=-" in res.output
