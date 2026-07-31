"""Agent registry — issue #607."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from vouch import agents, audit, trust
from vouch.agents import AgentError, AgentStatus
from vouch.cli import cli
from vouch.scopes import PROPOSE, READ
from vouch.storage import KBStore

TOKEN = "s3cret-token-example"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    kb = KBStore.init(tmp_path)
    monkeypatch.chdir(kb.root)
    return kb


@pytest.fixture
def subject() -> str:
    return trust.auth_subject_for_token(TOKEN)


def _register(store: KBStore, subject: str, name: str = "ci-bot"):
    return agents.register(store, subject=subject, name=name, actor="human")


# --- the registry ---------------------------------------------------------


def test_register_names_a_subject_without_storing_the_token(
    store: KBStore, subject: str
) -> None:
    """The registry is committed, so it must never contain the credential."""
    agent = _register(store, subject)
    assert agent.name == "ci-bot"
    assert agent.status is AgentStatus.ACTIVE
    assert agent.claimed_at is not None

    raw = (store.kb_dir / agents.REGISTRY_FILENAME).read_text(encoding="utf-8")
    assert subject in raw
    assert TOKEN not in raw


def test_registry_round_trips(store: KBStore, subject: str) -> None:
    agents.register(
        store, subject=subject, name="ci-bot", actor="human",
        scopes=(READ, PROPOSE), note="the CI proposer",
    )
    loaded = agents.load_registry(store)
    assert len(loaded) == 1
    assert loaded[0].scopes == (READ, PROPOSE)
    assert loaded[0].note == "the CI proposer"


def test_find_by_name_or_subject(store: KBStore, subject: str) -> None:
    _register(store, subject)
    assert agents.find(store, "ci-bot") is not None
    assert agents.find(store, subject) is not None
    assert agents.find(store, "nobody") is None
    assert agents.find(store, "   ") is None


def test_duplicate_name_is_refused(store: KBStore, subject: str) -> None:
    _register(store, subject)
    with pytest.raises(AgentError, match="already registered to subject"):
        agents.register(store, subject="other-subject", name="ci-bot", actor="human")


def test_duplicate_subject_is_refused(store: KBStore, subject: str) -> None:
    _register(store, subject)
    with pytest.raises(AgentError, match="already registered as"):
        agents.register(store, subject=subject, name="other-bot", actor="human")


@pytest.mark.parametrize(
    ("subject_arg", "name", "match"),
    [("", "ci-bot", "auth subject"), ("s", "  ", "needs a name")],
)
def test_register_validates_its_inputs(
    store: KBStore, subject_arg: str, name: str, match: str
) -> None:
    with pytest.raises(AgentError, match=match):
        agents.register(store, subject=subject_arg, name=name, actor="human")


# --- status transitions ---------------------------------------------------


def test_pause_and_resume_round_trip(store: KBStore, subject: str) -> None:
    _register(store, subject)
    assert agents.set_status(
        store, "ci-bot", AgentStatus.PAUSED, actor="human"
    ).status is AgentStatus.PAUSED
    assert agents.set_status(
        store, "ci-bot", AgentStatus.ACTIVE, actor="human"
    ).status is AgentStatus.ACTIVE


def test_revocation_is_terminal(store: KBStore, subject: str) -> None:
    """An undo button on revocation is a footgun, so there isn't one."""
    _register(store, subject)
    agents.set_status(store, "ci-bot", AgentStatus.REVOKED, actor="human")

    for status in (AgentStatus.ACTIVE, AgentStatus.PAUSED):
        with pytest.raises(AgentError, match="revocation is terminal"):
            agents.set_status(store, "ci-bot", status, actor="human")


def test_setting_the_current_status_is_a_no_op(store: KBStore, subject: str) -> None:
    _register(store, subject)
    before = len(list(audit.read_events(store.kb_dir)))
    agents.set_status(store, "ci-bot", AgentStatus.ACTIVE, actor="human")
    assert len(list(audit.read_events(store.kb_dir))) == before


def test_unknown_agent_transition_is_refused(store: KBStore) -> None:
    with pytest.raises(AgentError, match="unknown agent"):
        agents.set_status(store, "nobody", AgentStatus.PAUSED, actor="human")


@pytest.mark.parametrize(
    ("status", "event", "reversible"),
    [
        (AgentStatus.PAUSED, "agent.pause", True),
        (AgentStatus.REVOKED, "agent.revoke", False),
    ],
)
def test_every_transition_is_audited(
    store: KBStore, subject: str, status: AgentStatus, event: str, reversible: bool
) -> None:
    """The control plane's own history must be as auditable as the KB's."""
    _register(store, subject)
    agents.set_status(store, "ci-bot", status, actor="human")

    logged = [e for e in audit.read_events(store.kb_dir) if e.event == event]
    assert len(logged) == 1
    assert logged[0].actor == "human"
    assert logged[0].object_ids == [subject]
    assert logged[0].reversible is reversible


def test_registration_is_audited(store: KBStore, subject: str) -> None:
    agents.register(
        store, subject=subject, name="ci-bot", actor="human", scopes=(PROPOSE,)
    )
    ev = next(e for e in audit.read_events(store.kb_dir) if e.event == "agent.register")
    assert ev.data["name"] == "ci-bot"
    assert ev.data["scopes"] == [PROPOSE]


# --- the authentication gate ---------------------------------------------


def test_unregistered_subject_still_authenticates(
    store: KBStore, subject: str
) -> None:
    """Registration is opt-in, not a migration: old deployments keep working."""
    assert agents.is_active(store, subject) is True


@pytest.mark.parametrize(
    ("status", "allowed"),
    [(AgentStatus.ACTIVE, True), (AgentStatus.PAUSED, False),
     (AgentStatus.REVOKED, False)],
)
def test_is_active_follows_status(
    store: KBStore, subject: str, status: AgentStatus, allowed: bool
) -> None:
    _register(store, subject)
    if status is not AgentStatus.ACTIVE:
        agents.set_status(store, "ci-bot", status, actor="human")
    assert agents.is_active(store, subject) is allowed


def test_gate_denies_a_paused_token_at_the_chokepoint(
    store: KBStore, subject: str
) -> None:
    """A paused agent must be indistinguishable from a wrong token."""
    _register(store, subject)
    header = f"Bearer {TOKEN}"
    gate = lambda s: agents.is_active(store, s)  # noqa: E731

    assert trust.authorized_bearer_token(header, (TOKEN,), gate=gate) == TOKEN
    agents.set_status(store, "ci-bot", AgentStatus.PAUSED, actor="human")
    assert trust.authorized_bearer_token(header, (TOKEN,), gate=gate) is None


def test_gate_is_a_no_op_without_a_registry_gate() -> None:
    """With no gate the chokepoint is exactly the old behaviour."""
    header = f"Bearer {TOKEN}"
    assert trust.authorized_bearer_token(header, (TOKEN,)) == TOKEN
    assert trust.authorized_bearer_token("Bearer wrong", (TOKEN,)) is None
    assert trust.authorized_bearer_token(None, (TOKEN,)) is None


def test_gate_never_runs_for_a_token_that_did_not_match(store: KBStore) -> None:
    calls: list[str] = []

    def gate(subject: str) -> bool:
        calls.append(subject)
        return True

    assert trust.authorized_bearer_token("Bearer wrong", (TOKEN,), gate=gate) is None
    assert calls == []


def test_subject_is_active_allows_when_no_kb_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request with no KB has no registry to be denied by."""
    monkeypatch.chdir(tmp_path)
    assert agents.subject_is_active("whatever") is True


def test_subject_is_active_reads_the_discovered_registry(
    store: KBStore, subject: str
) -> None:
    _register(store, subject)
    assert agents.subject_is_active(subject) is True
    agents.set_status(store, "ci-bot", AgentStatus.REVOKED, actor="human")
    assert agents.subject_is_active(subject) is False


def test_http_transports_use_the_gated_chokepoint() -> None:
    """Both HTTP entry points must inherit revocation, not just one."""
    from pathlib import Path as _Path

    src = _Path("src/vouch/http_server.py").read_text(encoding="utf-8")
    assert src.count("authorized_bearer_token") == 2
    assert src.count("gate=agents_mod.subject_is_active") == 2
    assert "trust_mod.matched_bearer_token(" not in src


# --- a corrupt registry must not open the door ---------------------------


def test_unreadable_status_fails_closed(store: KBStore, subject: str) -> None:
    """A corrupted status must never read as active."""
    (store.kb_dir / agents.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"agents": [
            {"subject": subject, "name": "ci-bot", "status": "not-a-status"},
        ]}),
        encoding="utf-8",
    )
    assert agents.load_registry(store)[0].status is AgentStatus.REVOKED
    assert agents.is_active(store, subject) is False


def test_malformed_rows_are_skipped(store: KBStore, subject: str) -> None:
    (store.kb_dir / agents.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"agents": [
            "not-a-mapping",
            {"name": "no-subject"},
            {"subject": "no-name"},
            {"subject": subject, "name": "ci-bot", "scopes": "not-a-list",
             "claimed_at": "not-a-date"},
            {"subject": subject, "name": "duplicate-subject"},
        ]}),
        encoding="utf-8",
    )
    loaded = agents.load_registry(store)
    assert [a.name for a in loaded] == ["ci-bot"]
    assert loaded[0].scopes == ()
    assert loaded[0].claimed_at is None


def test_unreadable_registry_is_not_fatal(store: KBStore) -> None:
    (store.kb_dir / agents.REGISTRY_FILENAME).write_text("{{ not yaml", encoding="utf-8")
    assert agents.load_registry(store) == []


def test_registry_that_is_not_a_list_yields_nothing(store: KBStore) -> None:
    (store.kb_dir / agents.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"agents": {"not": "a list"}}), encoding="utf-8"
    )
    assert agents.load_registry(store) == []


def test_missing_registry_is_empty(store: KBStore) -> None:
    assert agents.load_registry(store) == []


# --- audit replay ---------------------------------------------------------


def test_replay_separates_what_the_agent_did_from_what_was_done_to_it(
    store: KBStore, subject: str
) -> None:
    from vouch import proposals

    _register(store, subject)
    agent = agents.find(store, "ci-bot")
    assert agent is not None

    src = store.put_source(b"the agent's own source bytes")
    proposals.propose_claim(
        store, text="auth uses jwt tokens", evidence=[src.id],
        proposed_by=agent.actor,
    )
    agents.set_status(store, "ci-bot", AgentStatus.PAUSED, actor="human")

    events = agents.replay(store, "ci-bot")
    did = [e for e in events if e["by_agent"]]
    done_to = [e for e in events if not e["by_agent"]]

    assert [e["event"] for e in did] == ["proposal.claim.create"]
    assert {e["event"] for e in done_to} == {"agent.register", "agent.pause"}


def test_replay_limit_keeps_the_newest(store: KBStore, subject: str) -> None:
    _register(store, subject)
    agents.set_status(store, "ci-bot", AgentStatus.PAUSED, actor="human")
    agents.set_status(store, "ci-bot", AgentStatus.ACTIVE, actor="human")

    assert len(agents.replay(store, "ci-bot", limit=1)) == 1
    assert agents.replay(store, "ci-bot", limit=1)[0]["event"] == "agent.resume"
    assert len(agents.replay(store, "ci-bot", limit=None)) == 3


def test_replay_of_an_unknown_agent_is_refused(store: KBStore) -> None:
    with pytest.raises(AgentError, match="unknown agent"):
        agents.replay(store, "nobody")


# --- cli ------------------------------------------------------------------


def test_cli_subject_prints_a_hash_not_the_token(store: KBStore) -> None:
    res = CliRunner().invoke(cli, ["agents", "subject", TOKEN])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == trust.auth_subject_for_token(TOKEN)
    assert TOKEN not in res.output


def test_cli_register_list_show_roundtrip(store: KBStore, subject: str) -> None:
    runner = CliRunner()

    empty = runner.invoke(cli, ["agents", "list"])
    assert empty.exit_code == 0
    assert "no registered agents" in empty.output

    reg = runner.invoke(cli, [
        "agents", "register", "ci-bot", "--subject", subject, "--scope", PROPOSE,
    ])
    assert reg.exit_code == 0, reg.output

    listed = runner.invoke(cli, ["agents", "list"])
    assert "ci-bot" in listed.output
    assert "active" in listed.output
    assert PROPOSE in listed.output

    shown = runner.invoke(cli, ["agents", "show", "ci-bot"])
    assert shown.exit_code == 0, shown.output
    assert "agent.register" in shown.output


def test_cli_pause_resume_revoke(store: KBStore, subject: str) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["agents", "register", "ci-bot", "--subject", subject])

    assert "paused" in runner.invoke(cli, ["agents", "pause", "ci-bot"]).output
    assert "resumed" in runner.invoke(cli, ["agents", "resume", "ci-bot"]).output

    revoked = runner.invoke(cli, ["agents", "revoke", "ci-bot", "--yes"])
    assert revoked.exit_code == 0, revoked.output
    assert "revoked" in revoked.output


def test_cli_revoke_requires_confirmation(store: KBStore, subject: str) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["agents", "register", "ci-bot", "--subject", subject])

    declined = runner.invoke(cli, ["agents", "revoke", "ci-bot"], input="n\n")
    assert declined.exit_code != 0
    assert agents.find(store, "ci-bot").status is AgentStatus.ACTIVE  # type: ignore[union-attr]


def test_cli_json_output(store: KBStore, subject: str) -> None:
    runner = CliRunner()
    runner.invoke(cli, ["agents", "register", "ci-bot", "--subject", subject])

    listed = json.loads(runner.invoke(cli, ["agents", "list", "--json"]).output)
    assert listed["agents"][0]["name"] == "ci-bot"

    shown = json.loads(
        runner.invoke(cli, ["agents", "show", "ci-bot", "--json"]).output
    )
    assert shown["agent"]["subject"] == subject
    assert shown["events"][0]["event"] == "agent.register"


def test_cli_domain_errors_are_clean(store: KBStore) -> None:
    """A domain error renders as `Error: ...`, never a traceback."""
    runner = CliRunner()
    for args in (
        ["agents", "pause", "nobody"],
        ["agents", "show", "nobody"],
    ):
        res = runner.invoke(cli, args)
        assert res.exit_code != 0
        assert "Error: unknown agent" in res.output
        assert "Traceback" not in res.output


def test_cli_show_reports_an_agent_with_no_events(
    store: KBStore, subject: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(store, subject)
    monkeypatch.setattr(agents, "replay", lambda *a, **k: [])
    res = CliRunner().invoke(cli, ["agents", "show", "ci-bot"])
    assert res.exit_code == 0, res.output
    assert "no audit events" in res.output


# --- last-used is derived, never stored -----------------------------------


def test_last_seen_is_derived_from_the_audit_log(
    store: KBStore, subject: str
) -> None:
    """Storing it would mean a disk write on every authenticated request."""
    from vouch import proposals

    _register(store, subject)
    agent = agents.find(store, "ci-bot")
    assert agent is not None
    assert agents.last_seen(store, agent) is None

    src = store.put_source(b"the agent's own source bytes")
    proposals.propose_claim(
        store, text="auth uses jwt tokens", evidence=[src.id],
        proposed_by=agent.actor,
    )
    seen = agents.last_seen(store, agent)
    assert seen is not None

    # and it is not persisted into the committed registry
    raw = (store.kb_dir / agents.REGISTRY_FILENAME).read_text(encoding="utf-8")
    assert "last_seen" not in raw


def test_last_seen_tracks_the_newest_event(store: KBStore, subject: str) -> None:
    from vouch import proposals

    _register(store, subject)
    agent = agents.find(store, "ci-bot")
    assert agent is not None
    src = store.put_source(b"the agent's own source bytes")
    proposals.propose_claim(
        store, text="first claim from the agent", evidence=[src.id],
        proposed_by=agent.actor,
    )
    first = agents.last_seen(store, agent)
    proposals.propose_claim(
        store, text="second claim from the agent", evidence=[src.id],
        proposed_by=agent.actor,
    )
    second = agents.last_seen(store, agent)
    assert first is not None and second is not None
    assert second >= first


def test_cli_list_reports_last_used(store: KBStore, subject: str) -> None:
    CliRunner().invoke(cli, ["agents", "register", "ci-bot", "--subject", subject])
    listed = CliRunner().invoke(cli, ["agents", "list"])
    assert "last-used=never" in listed.output

    payload = json.loads(
        CliRunner().invoke(cli, ["agents", "list", "--json"]).output
    )
    assert payload["agents"][0]["last_seen"] is None
