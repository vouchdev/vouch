"""Tests for the dual-solve web runner (the SPA backend)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vouch import dual_solve as ds
from vouch.storage import KBStore
from vouch.web import create_app

pytest.importorskip("fastapi", reason="dual-solve web needs the [web] extra")

from fastapi.testclient import TestClient


@pytest.fixture
def git_kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    # dual-solve needs a git repo; the kb lives at its root.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    s = KBStore.init(tmp_path)
    monkeypatch.chdir(s.root)
    return s


def _client(git_kb: KBStore, *, enabled: bool = True) -> TestClient:
    app = create_app(str(git_kb.root), allow_dual_solve=enabled)
    client = TestClient(app)
    # enter so the portal stays alive across requests; background tasks
    # (e.g. _run_job) can then run to completion without being cancelled when
    # the first request's ephemeral portal closes.
    client.__enter__()
    return client


def test_dual_solve_page_renders_when_enabled(git_kb):
    r = _client(git_kb).get("/dual-solve")
    assert r.status_code == 200
    assert "dual-solve-app" in r.text  # the Vue mount point


def test_dual_solve_routes_absent_when_disabled(git_kb):
    # the security gate: with --allow-dual-solve off, NOTHING mounts -- not just
    # the page, but the executing run/choose routes that spawn engines.
    c = _client(git_kb, enabled=False)
    assert c.get("/dual-solve").status_code == 404
    assert c.post("/dual-solve/run", json={"issue_url": "o/n#4"}).status_code == 404
    assert c.post("/dual-solve/choose",
                  json={"job_id": "x", "winner": None}).status_code == 404


def test_dual_solve_sandbox_runner_can_be_enabled(git_kb, monkeypatch):
    from vouch.web import dual_solve_api as api

    captured: dict = {}

    class FakeSandboxRunner:
        def __init__(self, *, repo_root, runner, image):
            captured["repo_root"] = repo_root
            captured["runner"] = runner
            captured["image"] = image

    monkeypatch.setattr(api, "require_docker_sandbox",
                        lambda image, runner=None: captured.update(required=image))
    monkeypatch.setattr(api, "DockerAgentRunner", FakeSandboxRunner)
    app = create_app(
        str(git_kb.root),
        allow_dual_solve=True,
        dual_solve_sandbox=True,
        dual_solve_sandbox_image="agent-img",
    )

    assert app.state.dual_solve_git_root == str(git_kb.root)
    assert captured["required"] == "agent-img"
    assert captured["repo_root"] == git_kb.root
    assert captured["image"] == "agent-img"


def _fake_prepare(monkeypatch, *, calls):
    issue = ds.Issue("Fix bug", "body", number=4, url="u")
    cA = ds.Candidate("claude", "vouch-dual/4-fix-bug-claude", Path("/w/claude"),
                      diff="diff --git a/x b/x\n+1\n", sha="s1", ok=True)
    cX = ds.Candidate("codex", "vouch-dual/4-fix-bug-codex", Path("/w/codex"),
                      diff="diff --git a/y b/y\n+2\n", sha="s2", ok=True)

    def fake(store, issue_ref, root, runner, *, claude_effort="high",
             codex_effort="high", autonomy="edit", dry_run=False,
             workdir=None, on_progress=None):
        calls.append({"autonomy": autonomy, "issue_ref": issue_ref})
        if on_progress:
            on_progress("running claude (effort=high)")
        return issue, [cA, cX], {"claude": object(), "codex": object()}

    monkeypatch.setattr("vouch.dual_solve.prepare", fake)
    return issue


def _wait(client, job_id, want, tries=50):
    import time
    for _ in range(tries):
        r = client.get(f"/dual-solve/job/{job_id}")
        if r.status_code == 200 and r.json()["status"] in want:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"job never reached {want}: {r.json()}")


def test_run_starts_job_and_reaches_ready(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    c = _client(git_kb)
    r = c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    assert r.status_code == 201
    job_id = r.json()["job_id"]
    state = _wait(c, job_id, {"ready"})
    assert [x["engine"] for x in state["candidates"]] == ["claude", "codex"]
    # autonomy is forced to edit regardless of input
    assert calls[0]["autonomy"] == "edit"


def test_run_is_single_flight(git_kb, monkeypatch):
    from vouch.web import dual_solve_api as api
    _fake_prepare(monkeypatch, calls=[])
    c = _client(git_kb)
    # construct an in-flight job as the precondition -> a new run is rejected.
    c.app.state.dual_solve_job = api.DualSolveJob(
        id="active", issue_url="o/n#1", claude_effort="high",
        codex_effort="high", status="running")
    r = c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    assert r.status_code == 409


def test_run_replaces_abandoned_ready_job_and_cleans_up(git_kb, monkeypatch):
    from pathlib import Path

    from vouch.web import dual_solve_api as api
    _fake_prepare(monkeypatch, calls=[])
    cleaned = {"n": 0}
    monkeypatch.setattr("vouch.dual_solve.cleanup",
                        lambda *a, **k: cleaned.__setitem__("n", cleaned["n"] + 1))
    c = _client(git_kb)
    stale = api.DualSolveJob(
        id="stale", issue_url="o/n#1", claude_effort="high",
        codex_effort="high", status="ready")
    stale.candidates = [ds.Candidate("claude", "b", Path("/w/c"), ok=True)]
    c.app.state.dual_solve_job = stale
    r = c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    assert r.status_code == 201       # an abandoned ready job is replaceable
    assert cleaned["n"] == 1          # its worktrees were cleaned up first


def test_run_rejects_unparseable_issue(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    r = _client(git_kb).post("/dual-solve/run", json={"issue_url": "not-an-issue"})
    assert r.status_code == 400


def test_progress_frame_reaches_websocket(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    c = _client(git_kb)
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
        seen = []
        for _ in range(10):
            frame = ws.receive_json()
            seen.append(frame)
            if frame.get("type") == "dual_solve" and frame.get("event") == "progress":
                break
        assert any(f.get("type") == "dual_solve" and f.get("event") == "progress"
                   for f in seen)


def _fake_finalize(monkeypatch, *, captured):
    def fake(store, root, issue, chosen, engines, candidates, reason, runner, *,
             record, proposed_by):
        captured.update(
            winner=(chosen.engine if chosen else None),
            record=record, reason=reason, proposed_by=proposed_by)
        return ["prop-1", "prop-2"] if chosen else []
    monkeypatch.setattr("vouch.dual_solve.finalize", fake)


def test_choose_winner_finalizes_and_returns_ids(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    captured: dict = {}
    _fake_finalize(monkeypatch, captured=captured)
    c = _client(git_kb)
    job_id = c.post("/dual-solve/run", json={"issue_url": "o/n#4"}).json()["job_id"]
    _wait(c, job_id, {"ready"})
    r = c.post("/dual-solve/choose",
               json={"job_id": job_id, "winner": "codex", "reason": "cleaner"})
    assert r.status_code == 200
    assert r.json()["proposed_ids"] == ["prop-1", "prop-2"]
    assert r.json()["kept_branch"] == "vouch-dual/4-fix-bug-codex"
    assert captured["winner"] == "codex"
    assert captured["record"] is True and captured["reason"] == "cleaner"


def test_choose_neither_records_nothing(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    captured: dict = {}
    _fake_finalize(monkeypatch, captured=captured)
    c = _client(git_kb)
    job_id = c.post("/dual-solve/run", json={"issue_url": "o/n#4"}).json()["job_id"]
    _wait(c, job_id, {"ready"})
    r = c.post("/dual-solve/choose",
               json={"job_id": job_id, "winner": None, "reason": ""})
    assert r.status_code == 200
    assert r.json()["proposed_ids"] == [] and r.json()["kept_branch"] is None
    assert captured["winner"] is None


def test_choose_unknown_job_is_not_found(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    _fake_finalize(monkeypatch, captured={})
    c = _client(git_kb)
    c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    # don't wait for ready: immediately choosing may race, so assert the guard
    # by choosing a bogus job id (never the active job).
    r = c.post("/dual-solve/choose",
               json={"job_id": "deadbeef", "winner": "claude", "reason": ""})
    assert r.status_code == 404


def test_choose_when_not_ready_is_conflict(git_kb, monkeypatch):
    from vouch.web import dual_solve_api as api
    _fake_prepare(monkeypatch, calls=[])
    _fake_finalize(monkeypatch, captured={})
    c = _client(git_kb)
    # a job that is mid-finalize cannot be chosen again.
    c.app.state.dual_solve_job = api.DualSolveJob(
        id="busy", issue_url="o/n#1", claude_effort="high",
        codex_effort="high", status="finalizing")
    r = c.post("/dual-solve/choose",
               json={"job_id": "busy", "winner": "claude", "reason": ""})
    assert r.status_code == 409


def test_routes_require_auth_when_enabled(git_kb, monkeypatch):
    app = create_app(str(git_kb.root), auth_token="sekret", allow_dual_solve=True)
    c = TestClient(app)
    assert c.get("/dual-solve").status_code == 401
    assert c.post("/dual-solve/run", json={"issue_url": "o/n#4"}).status_code == 401
    assert c.post("/dual-solve/choose",
                  json={"job_id": "x", "winner": None}).status_code == 401
    # with the token, the page renders
    ok = c.get("/dual-solve", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200


def test_spa_assets_are_served(git_kb):
    c = _client(git_kb)
    for path in ("/static/dual_solve.js", "/static/dual_solve.css",
                 "/static/vendor/vue.esm-browser.prod.js"):
        assert c.get(path).status_code == 200, path
    page = c.get("/dual-solve").text
    assert "/static/dual_solve.js" in page and "createApp" in page
