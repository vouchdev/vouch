"""`vouch capture ingest-codex` — codex rollout parsing + review-gated
ingest (vouchdev/vouch#387).

Codex persists sessions as rollout jsonl files instead of emitting live
hooks. The parser maps rollout records into capture observations; the
ingest path reuses the existing rollup so a codex session yields the same
kind of PENDING page proposal a claude session does, deduped on the
rollout's session id. Fixtures use placeholder data only (alice-example).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import codex_rollout as cr
from vouch.cli import cli
from vouch.models import ProposalKind, ProposalStatus
from vouch.storage import KBStore

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BASIC = FIXTURES / "rollout-basic.jsonl"
NO_META = FIXTURES / "rollout-no-meta.jsonl"
BASIC_SESSION = "0197aaaa-1111-7000-8000-000000000001"


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


# --- parser ----------------------------------------------------------------


def test_parse_basic_rollout_extracts_session_meta() -> None:
    session = cr.parse_rollout(BASIC)
    assert session.session_id == BASIC_SESSION
    assert session.cwd == "/home/alice-example/projects/acme-app"
    assert session.started_at == "2026-07-01T09:00:00.000Z"
    assert session.first_prompt == "add a health endpoint to the acme api"


def test_parse_maps_tool_calls_to_observations() -> None:
    session = cr.parse_rollout(BASIC)
    summaries = [o["summary"] for o in session.observations]
    # exec_command with a non-zero exit is marked failed, like live capture
    assert "Command failed: pytest -q" in summaries
    # ...and the same command succeeding later reads as a plain run
    assert "Ran: pytest -q" in summaries
    # an apply_patch heredoc through the shell surfaces as a file edit
    edit = next(o for o in session.observations if o["tool"] == "Edit")
    assert edit["files"] == ["src/acme/api.py"]
    assert edit["summary"] == "Edited api.py"
    # mcp/custom tools keep their own name
    assert any(o["tool"] == "kb_search" for o in session.observations)
    # session mechanics (update_plan) are not observations
    assert not any("update_plan" in o["tool"] for o in session.observations)


def test_parse_keeps_bash_cmd_for_notable_commands() -> None:
    session = cr.parse_rollout(BASIC)
    bash = [o for o in session.observations if o["tool"] == "Bash"]
    assert all(o.get("cmd") for o in bash)


def test_patch_observation_verbs() -> None:
    add = cr._patch_observation("*** Add File: src/new.py\n")
    assert add is not None and add["summary"] == "Created new.py"
    delete = cr._patch_observation("*** Delete File: src/old.py\n")
    assert delete is not None and delete["summary"] == "Deleted old.py"
    update = cr._patch_observation("*** Update File: src/mod.py\n")
    assert update is not None and update["summary"] == "Edited mod.py"
    mixed = cr._patch_observation("*** Add File: a.py\n*** Delete File: b.py\n")
    assert mixed is not None and mixed["summary"] == "Edited 2 files"


def test_parse_tolerates_unknown_record_types() -> None:
    # rollout-basic.jsonl includes a world_state record and a reasoning
    # item; both must be skipped, not fatal.
    session = cr.parse_rollout(BASIC)
    assert len(session.observations) == 4


def test_parse_without_session_meta_is_actionable_error() -> None:
    with pytest.raises(cr.CodexRolloutError, match="session_meta"):
        cr.parse_rollout(NO_META)


def test_parse_missing_file_is_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(cr.CodexRolloutError, match="cannot read"):
        cr.parse_rollout(tmp_path / "nope.jsonl")


def test_parse_zstd_compressed_is_actionable_error(tmp_path: Path) -> None:
    frame = tmp_path / "rollout-2026-07-01T09-00-00-x.jsonl"
    frame.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 16)
    with pytest.raises(cr.CodexRolloutError, match="zstd"):
        cr.parse_rollout(frame)


# --- ingest ----------------------------------------------------------------


def test_ingest_files_one_pending_page(store: KBStore) -> None:
    result = cr.ingest_rollout(store, BASIC, generated_at="2026-07-01T10:00:00Z")
    pid = result["summary_proposal_id"]
    assert pid is not None
    assert result["captured"] == 4
    pend = store.list_proposals(ProposalStatus.PENDING)
    assert [p.id for p in pend] == [pid]
    pr = pend[0]
    assert pr.kind == ProposalKind.PAGE
    assert pr.proposed_by == cr.CODEX_ACTOR
    assert pr.session_id == BASIC_SESSION
    assert pr.payload["type"] == "session"
    body = pr.payload["body"]
    assert "src/acme/api.py" in body
    assert "pytest -q" in body
    assert pr.payload["title"].startswith("session: add a health endpoint")
    assert "[acme-app]" in pr.payload["title"]


def test_ingest_respects_vouch_agent_env(store: KBStore, monkeypatch) -> None:
    monkeypatch.setenv("VOUCH_AGENT", "codex-nightly")
    cr.ingest_rollout(store, BASIC)
    pend = store.list_proposals(ProposalStatus.PENDING)
    assert pend[0].proposed_by == "codex-nightly"


def test_reingest_same_session_is_noop(store: KBStore) -> None:
    first = cr.ingest_rollout(store, BASIC)
    second = cr.ingest_rollout(store, BASIC)
    assert second["skipped"] == "already-ingested"
    assert second["summary_proposal_id"] == first["summary_proposal_id"]
    assert len(store.list_proposals(None)) == 1


def test_ingest_below_min_files_nothing(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  min_observations: 99\n")
    result = cr.ingest_rollout(store, BASIC)
    assert result["skipped"] == "below-min"
    assert result["summary_proposal_id"] is None
    assert store.list_proposals(None) == []


def test_ingest_noop_when_capture_disabled(store: KBStore) -> None:
    store.config_path.write_text("capture:\n  enabled: false\n")
    result = cr.ingest_rollout(store, BASIC)
    assert result["skipped"] == "disabled"
    assert store.list_proposals(None) == []


def test_ingest_never_writes_approved_content(store: KBStore) -> None:
    """The review gate stays intact: ingest files a proposal, not a page."""
    cr.ingest_rollout(store, BASIC)
    assert store.list_pages() == []


# --- --latest resolution ----------------------------------------------------


def _write_rollout(sessions: Path, day: str, stamp: str, sid: str, cwd: str) -> Path:
    d = sessions / day
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-{stamp}-{sid}.jsonl"
    meta = {
        "timestamp": f"{day.replace('/', '-')}T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": sid, "session_id": sid, "cwd": cwd},
    }
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


def test_find_latest_rollout_matches_project_cwd(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    sessions = home / "sessions"
    project = tmp_path / "proj"
    project.mkdir()
    other = "/home/alice-example/elsewhere"
    _write_rollout(sessions, "2026/07/01", "2026-07-01T08-00-00", "aaa", str(project))
    newest_match = _write_rollout(
        sessions, "2026/07/02", "2026-07-02T08-00-00", "bbb", str(project)
    )
    _write_rollout(sessions, "2026/07/03", "2026-07-03T08-00-00", "ccc", other)
    found = cr.find_latest_rollout(project, codex_home=home)
    assert found == newest_match


def test_find_latest_rollout_none_when_no_match(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    _write_rollout(home / "sessions", "2026/07/01", "2026-07-01T08-00-00",
                   "aaa", "/home/alice-example/elsewhere")
    project = tmp_path / "proj"
    project.mkdir()
    assert cr.find_latest_rollout(project, codex_home=home) is None


def test_find_latest_rollout_none_without_sessions_dir(tmp_path: Path) -> None:
    assert cr.find_latest_rollout(tmp_path, codex_home=tmp_path / "nope") is None


# --- CLI surface ------------------------------------------------------------


def test_cli_ingest_codex_files_proposal(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["capture", "ingest-codex", str(BASIC)])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["summary_proposal_id"]
    assert out["session_id"] == BASIC_SESSION


def test_cli_ingest_codex_reingest_reports_noop(store: KBStore, monkeypatch) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    runner = CliRunner()
    runner.invoke(cli, ["capture", "ingest-codex", str(BASIC)])
    res = runner.invoke(cli, ["capture", "ingest-codex", str(BASIC)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["skipped"] == "already-ingested"


def test_cli_ingest_codex_malformed_is_clean_error(
    store: KBStore, monkeypatch
) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(cli, ["capture", "ingest-codex", str(NO_META)])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "Error:" in res.output
    assert store.list_proposals(None) == []


def test_cli_ingest_codex_requires_exactly_one_source(
    store: KBStore, monkeypatch
) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    runner = CliRunner()
    neither = runner.invoke(cli, ["capture", "ingest-codex"])
    assert neither.exit_code != 0
    assert "exactly one" in neither.output
    both = runner.invoke(cli, ["capture", "ingest-codex", str(BASIC), "--latest"])
    assert both.exit_code != 0
    assert "exactly one" in both.output


def test_cli_ingest_codex_latest_resolves_for_project(
    store: KBStore, tmp_path: Path, monkeypatch
) -> None:
    project = store.kb_dir.parent
    home = tmp_path / "codex-home"
    sid = "0197bbbb-2222-7000-8000-000000000002"
    path = _write_rollout(
        home / "sessions", "2026/07/02", "2026-07-02T09-00-00", sid,
        str(project.resolve()),
    )
    # give the rollout enough activity to clear min_observations
    with path.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({
                "timestamp": "2026-07-02T09:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call", "name": "exec_command",
                    "arguments": json.dumps({"cmd": f"echo step-{i}"}),
                    "call_id": f"call_{i}",
                },
            }) + "\n")
    monkeypatch.chdir(project)
    res = CliRunner().invoke(
        cli, ["capture", "ingest-codex", "--latest", "--codex-home", str(home)],
    )
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["session_id"] == sid


def test_cli_ingest_codex_latest_no_rollout_is_clean_error(
    store: KBStore, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(store.kb_dir.parent)
    res = CliRunner().invoke(
        cli,
        ["capture", "ingest-codex", "--latest", "--codex-home",
         str(tmp_path / "empty-home")],
    )
    assert res.exit_code != 0
    assert "no codex rollout found" in res.output


def test_fixtures_use_placeholder_data_only() -> None:
    """Privacy rule: fixture rollouts must not carry real paths or names."""
    for fixture in FIXTURES.glob("*.jsonl"):
        text = fixture.read_text(encoding="utf-8")
        assert "alice-example" in text or "session_meta" not in text
        assert "/home/a/" not in text
