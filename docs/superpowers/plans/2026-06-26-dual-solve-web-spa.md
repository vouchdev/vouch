# dual-solve web SPA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ship a single-page app inside vouch's review-ui that takes a github issue link, runs `dual-solve` against the repo the server lives in, streams progress live, shows both engines' diffs side by side, and lets a human pick the winner — keeping the branch and proposing the rationale into the kb through the existing review gate.

**Architecture:** a new `/dual-solve*` route group mounted into the existing FastAPI app (`src/vouch/web/server.py:build_app`), only when launched with `vouch review-ui --allow-dual-solve`. The blocking `dual_solve.prepare`/`finalize` run in `run_in_threadpool`; phase progress (the existing `on_progress` hook) is bridged to the existing `_Hub` websocket via `asyncio.run_coroutine_threadsafe`. One in-process job at a time. The frontend is a buildless Vue 3 SPA served as static files.

**Tech Stack:** Python 3 / FastAPI / Starlette (`run_in_threadpool`, websockets) — all already in the `[web]` extra. Frontend: Vue 3 ESM browser build, **vendored**, no npm/bundler.

## Global Constraints

- Shipped feature in `src/vouch/web`; the **review-gate invariant is preserved** — web finalize only calls `proposals.propose_claim` (lands in `proposed/`), never `approve`.
- **Buildless Vue 3 only** — vendored `static/vendor/vue.esm-browser.prod.js` (full build, with template compiler). No npm, bundler, or build step enters the repo.
- The `/dual-solve*` routes mount **only** under `vouch review-ui --allow-dual-solve`; otherwise every such path is `404`.
- **`autonomy` is hard-forced to `"edit"`** in every web call to `dual_solve.prepare`; the `full`/bypassPermissions path is unreachable over HTTP.
- All dual-solve routes use the existing `guarded` dependency (Bearer-token auth) — no new auth path.
- Routes call `dual_solve.prepare`/`finalize`/`repo_root`/`cleanup` **through the `vouch.dual_solve` module object** (`from .. import dual_solve as ds`; `ds.prepare(...)`), so tests monkeypatch `vouch.dual_solve.*`.
- **import-as-used**: each task imports only the symbols its own code references, so `ruff` (F401) stays green at every commit; keep `__all__` isort-sorted (RUF022).
- Tooling: run `.venv/bin/pytest tests/ -q --ignore=tests/embeddings`, `.venv/bin/mypy src`, `.venv/bin/ruff check src tests`. **Never** the `python -m` form (a commit-validate hook mis-parses `-m`).
- Web tests live behind the `[web]` extra: start the module with `pytest.importorskip("fastapi", ...)` and import `TestClient` after it.
- Stage files **by name** (never `git add -A`). Conventional commits, lowercase body, **no `Co-Authored-By` trailer**.
- No new Python dependency. Vue is a vendored static asset, not a Python dep.

## File Structure

| kind | path | responsibility |
|---|---|---|
| new | `src/vouch/web/dual_solve_api.py` | `register(app, *, store, hub, auth, guarded, render, reviewer, enabled)`; `DualSolveJob`; the route group |
| new | `src/vouch/web/templates/dual_solve.html` | SPA shell (extends `base.html`, mounts Vue) |
| new | `src/vouch/web/static/dual_solve.js` | the Vue 3 app |
| new | `src/vouch/web/static/dual_solve.css` | two-pane diff layout |
| new | `src/vouch/web/static/vendor/vue.esm-browser.prod.js` | pinned Vue 3 (vendored) |
| new | `src/vouch/web/static/vendor/VENDOR.md` | vendored version + sha256 |
| modify | `src/vouch/web/server.py` | add `allow_dual_solve` param; call `register(...)`; inject `dual_solve_enabled` into templates |
| modify | `src/vouch/web/__init__.py` | thread `allow_dual_solve` through `create_app` |
| modify | `src/vouch/web/templates/base.html` | conditional `dual-solve` nav link |
| modify | `src/vouch/cli.py` (`review-ui`) | add `--allow-dual-solve` flag |
| new | `proposals/0001-dual-solve-web.md` | the VEP (governance) |
| new | `tests/test_web_dual_solve.py` | TestClient lifecycle + auth + gate + ws |
| modify | `CHANGELOG.md` | `[Unreleased] / ### Added` |

Reference signatures (verified against source, do not re-derive):
- `dual_solve.prepare(store, issue_ref, root, runner, *, claude_effort="high", codex_effort="high", autonomy="edit", dry_run=False, workdir=None, on_progress=None) -> tuple[Issue, list[Candidate], dict[str, Engine]]`
- `dual_solve.finalize(store, root, issue, chosen, engines, candidates, reason, runner, *, record, proposed_by) -> list[str]`
- `dual_solve.repo_root(runner, cwd) -> Path` (raises `RuntimeError` outside a git repo)
- `dual_solve.cleanup(root, candidates, keep_branches, runner) -> None`
- `Candidate(engine, branch, worktree, diff="", sha="", ok=False, error=None)`
- `SubprocessRunner` lives in `vouch.auto_pr`.
- `build_app(kb_root=None, *, auth=None, page_size=DEFAULT_PAGE_SIZE) -> FastAPI`; inside it: `store`, `hub` (`_Hub`), `auth`, `guarded` (`[Depends(require_auth)]`), `_tmpl(request, name, ctx)`, `reviewer()`.

---

### Task 1: VEP governance doc

**Files:**
- Create: `proposals/0001-dual-solve-web.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the accepted-surface rationale the PR links to. No code symbols.

This is a documentation deliverable (no pytest). The reviewer checks content coverage, not a test run.

- [ ] **Step 1: Look at the proposals format**

Run: `ls proposals/ && sed -n '1,40p' proposals/README.md`
Expected: see the house format for a VEP (title, status, motivation, design, trust-boundary).

- [ ] **Step 2: Write the VEP**

Create `proposals/0001-dual-solve-web.md` (match the README's headings; lowercase prose). It MUST cover:
- **motivation**: a browser surface to run dual-solve and pick a winner without the terminal.
- **surface added**: the `/dual-solve`, `/dual-solve/run`, `/dual-solve/job/{id}`, `/dual-solve/choose` routes + reuse of `/ws`.
- **why it's safe**: off by default (`--allow-dual-solve`), localhost-first, Bearer-token guarded, **edit-only over HTTP** (no `full` autonomy), and the **review-gate invariant holds** — web finalize only `propose`s, nothing auto-approves.
- **out of scope**: on-demand cloning, `full` autonomy, multi-job concurrency.

- [ ] **Step 3: Commit**

```bash
git add proposals/0001-dual-solve-web.md
git commit -F <msg-file>   # docs(dual-solve-web): vep for the web runner surface
```

---

### Task 2: gate + plumbing + SPA shell route

**Files:**
- Create: `src/vouch/web/dual_solve_api.py`
- Modify: `src/vouch/web/server.py`, `src/vouch/web/__init__.py`, `src/vouch/web/templates/base.html`, `src/vouch/cli.py`
- Create: `src/vouch/web/templates/dual_solve.html`
- Test: `tests/test_web_dual_solve.py`

**Interfaces:**
- Consumes: `build_app` internals (`store`, `hub`, `auth`, `guarded`, `_tmpl`, `reviewer`); `dual_solve.repo_root`; `SubprocessRunner`.
- Produces: `register(app, *, store, hub, auth, guarded, render, reviewer, enabled) -> None`. When `enabled` is false it returns immediately (mounts nothing). When true it derives `git_root = ds.repo_root(SubprocessRunner(), store.root)` and mounts `GET /dual-solve` (the SPA shell). `create_app(..., allow_dual_solve=False)` and `build_app(..., allow_dual_solve=False)` gain the keyword. `vouch review-ui --allow-dual-solve` sets it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_dual_solve.py`:

```python
"""Tests for the dual-solve web runner (the SPA backend)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    return TestClient(app)


def test_dual_solve_page_renders_when_enabled(git_kb):
    r = _client(git_kb).get("/dual-solve")
    assert r.status_code == 200
    assert "dual-solve-app" in r.text  # the Vue mount point


def test_dual_solve_routes_absent_when_disabled(git_kb):
    r = _client(git_kb, enabled=False).get("/dual-solve")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -q`
Expected: FAIL — `create_app()` has no `allow_dual_solve` keyword.

- [ ] **Step 3: Create the route module (shell route only)**

Create `src/vouch/web/dual_solve_api.py`:

```python
"""dual-solve web runner: routes mounted into the review-ui under an explicit
opt-in flag. The blocking engine work runs in a threadpool; progress streams
over the review-ui's existing websocket. The review gate is preserved -- the
choose step only ever proposes to the kb.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .. import dual_solve as ds
from ..auto_pr import SubprocessRunner


def register(
    app: FastAPI,
    *,
    store: Any,
    hub: Any,
    auth: Any,
    guarded: list,
    render: Callable[[Request, str, dict[str, Any]], Any],
    reviewer: Callable[[], str],
    enabled: bool,
) -> None:
    """Mount the dual-solve routes. No-op unless ``enabled``."""
    if not enabled:
        return
    runner = SubprocessRunner()
    # fail fast at app-build time if we're not in a git repo: dual-solve can't
    # create worktrees otherwise.
    git_root = ds.repo_root(runner, store.root)
    app.state.dual_solve_git_root = str(git_root)

    @app.get("/dual-solve", response_class=HTMLResponse, dependencies=guarded)
    def dual_solve_page(request: Request) -> Any:
        return render(request, "dual_solve.html", {"active": "dual-solve"})
```

- [ ] **Step 4: Create the SPA shell template**

Create `src/vouch/web/templates/dual_solve.html`:

```html
{% extends "base.html" %}
{% block title %}dual-solve · vouch{% endblock %}
{% block content %}
<div id="dual-solve-app"></div>
<script type="module">
  import { createApp } from "/static/vendor/vue.esm-browser.prod.js";
  import App from "/static/dual_solve.js";
  createApp(App).mount("#dual-solve-app");
</script>
{% endblock %}
```

(The referenced `/static/dual_solve.js` + vendored Vue are created in Task 5; the route still renders the shell HTML now.)

- [ ] **Step 5: Wire the flag through build_app**

In `src/vouch/web/server.py`, change the `build_app` signature to add the keyword:

```python
def build_app(
    kb_root: str | None = None,
    *,
    auth: AuthConfig | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    allow_dual_solve: bool = False,
) -> FastAPI:
```

Add `from .dual_solve_api import register as _register_dual_solve` to the module imports. Inject the nav flag into every template by editing the `_tmpl` helper:

```python
    def _tmpl(request: Request, name: str, ctx: dict[str, Any]) -> Any:
        ctx.setdefault("auth_enabled", auth.enabled)
        ctx.setdefault("dual_solve_enabled", allow_dual_solve)
        return templates.TemplateResponse(request, name, ctx)
```

Immediately before `return app`, mount the routes:

```python
    _register_dual_solve(
        app, store=store, hub=hub, auth=auth, guarded=guarded,
        render=_tmpl, reviewer=reviewer, enabled=allow_dual_solve,
    )
    return app
```

- [ ] **Step 6: Thread it through create_app**

In `src/vouch/web/__init__.py`, add the keyword to `create_app` and pass it down:

```python
def create_app(  # type: ignore[no-untyped-def]
    kb_root: str | None = None,
    *,
    auth_token: str | None = None,
    auth_label: str = "web-reviewer",
    page_size: int | None = None,
    allow_dual_solve: bool = False,
):
    _require_web_extra()
    from .server import DEFAULT_PAGE_SIZE, AuthConfig, build_app

    auth = AuthConfig(token=auth_token, label=auth_label)
    return build_app(
        kb_root,
        auth=auth,
        page_size=page_size if page_size is not None else DEFAULT_PAGE_SIZE,
        allow_dual_solve=allow_dual_solve,
    )
```

- [ ] **Step 7: Add the nav link**

In `src/vouch/web/templates/base.html`, inside `<nav class="nav">`, after the audit link:

```html
      {% if dual_solve_enabled %}<a href="/dual-solve" {% if active == "dual-solve" %}aria-current="page"{% endif %}>dual-solve</a>{% endif %}
```

- [ ] **Step 8: Add the CLI flag**

In `src/vouch/cli.py`, on the `review-ui` command (near line 2661), add an option and pass it to `create_app`. Add:

```python
@click.option(
    "--allow-dual-solve",
    is_flag=True,
    help="Mount the dual-solve runner SPA (spawns claude+codex; edit-only). "
         "Off by default; the server must run inside the target git repo.",
)
```

Add `allow_dual_solve: bool` to the `review_ui(...)` parameters and pass `allow_dual_solve=allow_dual_solve` into the `create_app(...)` call in that function.

- [ ] **Step 9: Run tests to verify they pass + gate**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -q`
Expected: PASS (2 tests).
Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/vouch/web/dual_solve_api.py src/vouch/web/templates/dual_solve.html \
        src/vouch/web/server.py src/vouch/web/__init__.py \
        src/vouch/web/templates/base.html src/vouch/cli.py tests/test_web_dual_solve.py
git commit -F <msg-file>   # feat(dual-solve-web): gate + spa shell route behind --allow-dual-solve
```

---

### Task 3: run endpoint + job model + progress bridge

**Files:**
- Modify: `src/vouch/web/dual_solve_api.py`
- Test: `tests/test_web_dual_solve.py`

**Interfaces:**
- Consumes: `dual_solve.prepare`, `dual_solve.cleanup`, the `_Hub.broadcast` coroutine.
- Produces: a `DualSolveJob` dataclass on `app.state.dual_solve_job`; `POST /dual-solve/run` returning `{"job_id": str}` (status 201); a `_serialize(job) -> dict` used by Task 4. Job `status` ∈ `{"running","ready","finalizing","done","error"}`. `prepare` is always called with `autonomy="edit"`.

- [ ] **Step 1: Write the failing tests**

First add `from vouch import dual_solve as ds` to the test file's top-level imports (it is used by the `_fake_prepare` helper below). Then append to `tests/test_web_dual_solve.py`:

```python
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
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    c = _client(git_kb)
    first = c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    assert first.status_code == 201
    # a second run while the first is active is rejected
    second = c.post("/dual-solve/run", json={"issue_url": "o/n#4"})
    assert second.status_code == 409


def test_run_rejects_unparseable_issue(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    r = _client(git_kb).post("/dual-solve/run", json={"issue_url": "not-an-issue"})
    assert r.status_code == 400
```

(`GET /dual-solve/job/{id}` is created in this task too — it's needed for `_wait`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -k "run_ or reaches" -q`
Expected: FAIL — no `/dual-solve/run` route.

- [ ] **Step 3: Implement the job model + run + bridge + job-state**

Edit `src/vouch/web/dual_solve_api.py`. Extend the imports (import-as-used):

```python
import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import dual_solve as ds
from ..auto_pr import SubprocessRunner
```

Add the job model + request body + helpers at module level:

```python
@dataclass
class DualSolveJob:
    id: str
    issue_url: str
    claude_effort: str
    codex_effort: str
    status: str = "running"
    progress: list[str] = field(default_factory=list)
    issue: Any = None
    candidates: list = field(default_factory=list)
    engines: dict = field(default_factory=dict)
    proposed_ids: list[str] = field(default_factory=list)
    kept_branch: str | None = None
    error: str | None = None


class _RunReq(BaseModel):
    issue_url: str
    claude_effort: str = "high"
    codex_effort: str = "high"


def _serialize(job: DualSolveJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "issue_url": job.issue_url,
        "status": job.status,
        "progress": list(job.progress),
        "issue": (
            {"number": job.issue.number, "title": job.issue.title,
             "url": job.issue.url}
            if job.issue is not None else None
        ),
        "candidates": [
            {"engine": c.engine, "branch": c.branch, "ok": c.ok,
             "error": c.error, "diff": c.diff}
            for c in job.candidates
        ],
        "proposed_ids": list(job.proposed_ids),
        "kept_branch": job.kept_branch,
        "error": job.error,
    }
```

Inside `register`, after the `dual_solve_page` route, add:

```python
    def _frame(job: DualSolveJob, event: str, message: str = "") -> dict[str, Any]:
        return {"type": "dual_solve", "job_id": job.id, "event": event,
                "message": message}

    async def _run_job(job: DualSolveJob, loop: asyncio.AbstractEventLoop) -> None:
        def on_progress(msg: str) -> None:
            # called from the threadpool worker; bridge to the async hub.
            job.progress.append(msg)
            asyncio.run_coroutine_threadsafe(
                hub.broadcast(_frame(job, "progress", msg)), loop)
        try:
            issue, candidates, engines = await run_in_threadpool(
                ds.prepare, store, job.issue_url, git_root, runner,
                claude_effort=job.claude_effort, codex_effort=job.codex_effort,
                autonomy="edit", on_progress=on_progress,
            )
            job.issue, job.candidates, job.engines = issue, candidates, engines
            job.status = "ready"
            await hub.broadcast(_frame(job, "ready"))
        except Exception as exc:
            # broad on purpose: any prepare failure must mark the job as errored
            # and notify, never crash the background task. (BLE is not in the
            # ruff ruleset, so no noqa is needed or wanted here.)
            job.status = "error"
            job.error = str(exc)
            await hub.broadcast(_frame(job, "error", str(exc)))

    @app.post("/dual-solve/run", status_code=201, dependencies=guarded)
    async def dual_solve_run(req: _RunReq) -> dict[str, str]:
        active = getattr(app.state, "dual_solve_job", None)
        if active is not None and active.status in ("running", "finalizing"):
            raise HTTPException(409, "a dual-solve job is already running")
        try:
            ds.parse_issue_ref(req.issue_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # clean up a stale prior job's worktrees so they don't leak.
        if active is not None and active.candidates:
            keep = {active.kept_branch} if active.kept_branch else set()
            await run_in_threadpool(
                ds.cleanup, git_root, active.candidates, keep, runner)
        job = DualSolveJob(
            id=uuid.uuid4().hex, issue_url=req.issue_url,
            claude_effort=req.claude_effort, codex_effort=req.codex_effort,
        )
        app.state.dual_solve_job = job
        loop = asyncio.get_running_loop()
        asyncio.create_task(_run_job(job, loop))
        return {"job_id": job.id}

    @app.get("/dual-solve/job/{job_id}", dependencies=guarded)
    def dual_solve_job(job_id: str) -> dict[str, Any]:
        job = getattr(app.state, "dual_solve_job", None)
        if job is None or job.id != job_id:
            raise HTTPException(404, "no such job")
        return _serialize(job)
```

Note: `git_root` here is the `Path` from `ds.repo_root`; pass it directly to `ds.prepare`/`ds.cleanup` (they take a `Path`). Keep the `app.state.dual_solve_git_root = str(git_root)` line from Task 2.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -q`
Expected: PASS (all tasks-so-far tests).

- [ ] **Step 5: Add the websocket-progress test**

Append:

```python
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
```

Run: `.venv/bin/pytest tests/test_web_dual_solve.py::test_progress_frame_reaches_websocket -q`
Expected: PASS (the sync→async bridge delivers the frame).

- [ ] **Step 6: Gate + commit**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`
Expected: clean.

```bash
git add src/vouch/web/dual_solve_api.py tests/test_web_dual_solve.py
git commit -F <msg-file>   # feat(dual-solve-web): run endpoint, job model, ws progress bridge
```

---

### Task 4: choose endpoint → finalize (keep branch + propose to kb)

**Files:**
- Modify: `src/vouch/web/dual_solve_api.py`
- Test: `tests/test_web_dual_solve.py`

**Interfaces:**
- Consumes: `dual_solve.finalize`; the `DualSolveJob` from Task 3.
- Produces: `POST /dual-solve/choose` taking `{job_id, winner, reason}` → `{"kept_branch": str|None, "proposed_ids": list[str]}`. `winner` ∈ `{"claude","codex",null}`; `null` keeps neither and records nothing. `finalize` is called with `record=True` and `proposed_by=reviewer()`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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


def test_choose_before_ready_is_conflict(git_kb, monkeypatch):
    calls: list = []
    _fake_prepare(monkeypatch, calls=calls)
    _fake_finalize(monkeypatch, captured={})
    c = _client(git_kb)
    job_id = c.post("/dual-solve/run", json={"issue_url": "o/n#4"}).json()["job_id"]
    # don't wait for ready: immediately choosing may race, so assert the guard
    # by choosing a bogus job id (never the active job).
    r = c.post("/dual-solve/choose",
               json={"job_id": "deadbeef", "winner": "claude", "reason": ""})
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -k choose -q`
Expected: FAIL — no `/dual-solve/choose` route.

- [ ] **Step 3: Implement the choose route**

Add the request body near `_RunReq`:

```python
class _ChooseReq(BaseModel):
    job_id: str
    winner: str | None = None
    reason: str = ""
```

Inside `register`, after `dual_solve_job`, add:

```python
    @app.post("/dual-solve/choose", dependencies=guarded)
    async def dual_solve_choose(req: _ChooseReq) -> dict[str, Any]:
        job = getattr(app.state, "dual_solve_job", None)
        if job is None or job.id != req.job_id:
            raise HTTPException(404, "no such job")
        if job.status != "ready":
            raise HTTPException(409, f"job is {job.status}, not ready")
        chosen = next((c for c in job.candidates if c.engine == req.winner), None)
        job.status = "finalizing"
        try:
            ids = await run_in_threadpool(
                ds.finalize, store, git_root, job.issue, chosen, job.engines,
                job.candidates, req.reason or "", runner,
                record=True, proposed_by=reviewer(),
            )
        except (ValueError, RuntimeError) as exc:
            job.status = "error"
            job.error = str(exc)
            raise HTTPException(500, f"finalize failed: {exc}") from exc
        job.proposed_ids = ids
        job.kept_branch = chosen.branch if chosen is not None else None
        job.status = "done"
        await hub.broadcast(_frame(job, "done"))
        return {"kept_branch": job.kept_branch, "proposed_ids": ids}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -k choose -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Add the auth-guard test**

Append (proves every dual-solve route is behind the Bearer gate when auth is enabled):

```python
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
```

Run: `.venv/bin/pytest tests/test_web_dual_solve.py -q`
Expected: PASS (all).

- [ ] **Step 6: Gate + commit**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src`
Expected: clean.

```bash
git add src/vouch/web/dual_solve_api.py tests/test_web_dual_solve.py
git commit -F <msg-file>   # feat(dual-solve-web): choose endpoint finalizes and proposes to kb
```

---

### Task 5: the Vue 3 SPA (buildless)

**Files:**
- Create: `src/vouch/web/static/vendor/vue.esm-browser.prod.js`, `src/vouch/web/static/vendor/VENDOR.md`
- Create: `src/vouch/web/static/dual_solve.js`, `src/vouch/web/static/dual_solve.css`
- Modify: `src/vouch/web/templates/dual_solve.html` (link the css)
- Test: `tests/test_web_dual_solve.py` (assets-served assertions)

**Interfaces:**
- Consumes: the `/dual-solve/run`, `/dual-solve/job/{id}`, `/dual-solve/choose` routes; `/ws`.
- Produces: a mounted Vue app (no Python symbols). Verified by an asset-serving test + a manual checklist (JS has no unit-test runner in this repo — do not add one; that's a build-step we explicitly excluded).

- [ ] **Step 1: Vendor Vue 3 (pinned)**

```bash
mkdir -p src/vouch/web/static/vendor
curl -fsSL https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js \
  -o src/vouch/web/static/vendor/vue.esm-browser.prod.js
sha256sum src/vouch/web/static/vendor/vue.esm-browser.prod.js
```

Create `src/vouch/web/static/vendor/VENDOR.md`:

```markdown
# vendored frontend assets

- `vue.esm-browser.prod.js` — Vue 3.4.38, full build (includes the template
  compiler so `template:` strings compile at runtime). Source:
  https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js
  sha256: <paste the sha256sum output here>

  buildless on purpose: no npm/bundler. bump = re-download a pinned version,
  update this file's version + sha, and re-run the manual smoke checklist.
```

- [ ] **Step 2: Write the SPA**

Create `src/vouch/web/static/dual_solve.js`:

```javascript
import { ref, reactive, onMounted } from "/static/vendor/vue.esm-browser.prod.js";

// Minimal unified-diff parser: groups lines per file with +/-/context classes.
function parseDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { path: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (!cur) {
      continue;
    } else if (line.startsWith("+++") || line.startsWith("---") ||
               line.startsWith("index ")) {
      continue;
    } else if (line.startsWith("@@")) {
      cur.lines.push({ cls: "hunk", text: line });
    } else if (line.startsWith("+")) {
      cur.lines.push({ cls: "add", text: line });
    } else if (line.startsWith("-")) {
      cur.lines.push({ cls: "del", text: line });
    } else {
      cur.lines.push({ cls: "ctx", text: line });
    }
  }
  return files;
}

export default {
  setup() {
    const issueUrl = ref("");
    const claudeEffort = ref("high");
    const codexEffort = ref("high");
    const reason = ref("");
    const job = reactive({
      id: null, status: "idle", progress: [], candidates: [],
      issue: null, error: null, kept_branch: null, proposed_ids: [],
    });

    function applyState(s) {
      Object.assign(job, s, {
        candidates: (s.candidates || []).map(
          (c) => ({ ...c, files: parseDiff(c.diff) })),
      });
    }
    async function refresh() {
      if (!job.id) return;
      const r = await fetch(`/dual-solve/job/${job.id}`);
      if (r.ok) applyState(await r.json());
    }
    function connectWs() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws`);
      ws.onmessage = (ev) => {
        let f;
        try { f = JSON.parse(ev.data); } catch { return; }
        if (f.type !== "dual_solve" || f.job_id !== job.id) return;
        if (f.event === "progress") job.progress.push(f.message);
        else refresh();   // ready/done/error -> pull the full state
      };
    }
    async function run() {
      job.progress = []; job.error = null; job.candidates = [];
      job.kept_branch = null; job.proposed_ids = [];
      const r = await fetch("/dual-solve/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          issue_url: issueUrl.value,
          claude_effort: claudeEffort.value,
          codex_effort: codexEffort.value,
        }),
      });
      if (!r.ok) { job.error = `run failed (${r.status})`; return; }
      job.id = (await r.json()).job_id;
      job.status = "running";
      await refresh();
    }
    async function choose(winner) {
      job.status = "finalizing";
      const r = await fetch("/dual-solve/choose", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: job.id, winner, reason: reason.value }),
      });
      if (!r.ok) { job.error = `choose failed (${r.status})`; return; }
      await refresh();
    }

    onMounted(connectWs);
    return { issueUrl, claudeEffort, codexEffort, reason, job, run, choose };
  },
  template: `
<section class="ds">
  <h1>dual-solve</h1>
  <form class="ds-run" @submit.prevent="run">
    <input v-model="issueUrl" placeholder="github issue url or owner/name#42"
           :disabled="job.status==='running'||job.status==='finalizing'" />
    <select v-model="claudeEffort"><option>low</option><option>medium</option><option selected>high</option><option>max</option></select>
    <select v-model="codexEffort"><option>low</option><option>medium</option><option selected>high</option><option>max</option></select>
    <button :disabled="!issueUrl||job.status==='running'||job.status==='finalizing'">run</button>
  </form>

  <p v-if="job.issue" class="ds-issue">#{{job.issue.number}} {{job.issue.title}}</p>
  <pre v-if="job.progress.length" class="ds-progress">{{ job.progress.join('\\n') }}</pre>
  <p v-if="job.error" class="ds-error">{{ job.error }}</p>

  <div v-if="job.status==='ready'||job.status==='done'" class="ds-panes">
    <div v-for="c in job.candidates" :key="c.engine" class="ds-pane">
      <h2>{{c.engine}} <small>{{c.branch}}</small></h2>
      <p v-if="!c.ok" class="ds-error">failed: {{c.error}}</p>
      <div v-for="f in c.files" :key="f.path" class="ds-file">
        <div class="ds-file-head">{{f.path}}</div>
        <pre><code><span v-for="(l,i) in f.lines" :key="i" :class="'ln-'+l.cls">{{l.text}}\\n</span></code></pre>
      </div>
    </div>
  </div>

  <div v-if="job.status==='ready'" class="ds-choose">
    <input v-model="reason" placeholder="one line: why this solution" />
    <button @click="choose('claude')">choose claude</button>
    <button @click="choose('codex')">choose codex</button>
    <button @click="choose(null)">keep neither</button>
  </div>

  <div v-if="job.status==='done'" class="ds-result">
    <p v-if="job.kept_branch">kept <code>{{job.kept_branch}}</code></p>
    <p v-for="pid in job.proposed_ids" :key="pid">
      proposed <a :href="'/'">{{pid}}</a> — review in the queue
    </p>
  </div>
</section>`,
};
```

- [ ] **Step 3: Style it**

Create `src/vouch/web/static/dual_solve.css` (two side-by-side panes; diff colors):

```css
.ds { max-width: 1200px; margin: 0 auto; }
.ds-run { display: flex; gap: .5rem; margin: 1rem 0; }
.ds-run input { flex: 1; }
.ds-progress { background:#111; color:#ddd; padding:.5rem; white-space:pre-wrap; }
.ds-error { color:#b00; }
.ds-panes { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
.ds-pane { border:1px solid #ccc; padding:.5rem; overflow:auto; }
.ds-file-head { font-weight:600; margin-top:.5rem; }
.ds-pane pre { margin:0; font-size:12px; overflow-x:auto; }
.ln-add { background:#e6ffed; display:block; }
.ln-del { background:#ffeef0; display:block; }
.ln-hunk { color:#06c; display:block; }
.ln-ctx { display:block; }
.ds-choose, .ds-result { margin:1rem 0; display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
```

- [ ] **Step 4: Link the css in the shell**

In `src/vouch/web/templates/dual_solve.html`, add a head block so the page pulls the stylesheet:

```html
{% block head %}<link rel="stylesheet" href="/static/dual_solve.css">{% endblock %}
```

If `base.html` has no `{% block head %}`, add one inside `<head>` (after the `app.css` link):

```html
  {% block head %}{% endblock %}
```

- [ ] **Step 5: Add the asset-serving test**

Append to `tests/test_web_dual_solve.py`:

```python
def test_spa_assets_are_served(git_kb):
    c = _client(git_kb)
    for path in ("/static/dual_solve.js", "/static/dual_solve.css",
                 "/static/vendor/vue.esm-browser.prod.js"):
        assert c.get(path).status_code == 200, path
    page = c.get("/dual-solve").text
    assert "/static/dual_solve.js" in page and "createApp" in page
```

Run: `.venv/bin/pytest tests/test_web_dual_solve.py::test_spa_assets_are_served -q`
Expected: PASS.

- [ ] **Step 6: Manual smoke (record the result in the commit body)**

```bash
# in a clone of a repo whose issue you want to solve, with the [web] extra:
.venv/bin/vouch review-ui --allow-dual-solve --no-open-browser
# open http://127.0.0.1:7780/dual-solve, paste an issue url, Run,
# watch progress, see both diffs, pick a winner, confirm "kept <branch>" +
# a "proposed <id>" link that lands in the queue.
```

- [ ] **Step 7: Gate + commit**

Run: `.venv/bin/ruff check src tests && .venv/bin/mypy src && .venv/bin/pytest tests/test_web_dual_solve.py -q`
Expected: clean / all pass.

```bash
git add src/vouch/web/static/vendor/vue.esm-browser.prod.js \
        src/vouch/web/static/vendor/VENDOR.md \
        src/vouch/web/static/dual_solve.js src/vouch/web/static/dual_solve.css \
        src/vouch/web/templates/dual_solve.html src/vouch/web/templates/base.html \
        tests/test_web_dual_solve.py
git commit -F <msg-file>   # feat(dual-solve-web): the buildless vue spa (run, diff panes, choose)
```

---

### Task 6: changelog + full gate

**Files:**
- Modify: `CHANGELOG.md`

**Interfaces:** none.

- [ ] **Step 1: Add the changelog entry**

In `CHANGELOG.md`, under `## [Unreleased]` / `### Added`, add:

```markdown
- `vouch review-ui --allow-dual-solve` — a browser SPA that runs `dual-solve`
  on a github issue link, streams progress, shows both engines' diffs side by
  side, and lets you pick the winner. Off by default; localhost-first;
  edit-only over http; the pick keeps the branch and proposes the rationale
  into the kb through the existing review gate (nothing auto-approves).
```

- [ ] **Step 2: Run the full CI gate**

Run: `.venv/bin/pytest tests/ -q --ignore=tests/embeddings`
Expected: green except the known pre-existing `test_volunteer_context::test_pending_proposal_not_volunteered` latin-1 failure.
Run: `.venv/bin/mypy src && .venv/bin/ruff check src tests`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -F <msg-file>   # docs(dual-solve-web): changelog entry for the runner spa
```

---

## Self-review notes (for the executor)

- **Spec coverage:** gate/off-by-default (Task 2), run+job+bridge (Task 3), job-state+choose+finalize+auth (Task 4), Vue SPA side-by-side+choose (Task 5), VEP (Task 1), changelog (Task 6). The "edit-only" constraint is asserted in Task 3's test; the review-gate invariant rides on `finalize(record=True)` only ever proposing (no approve call anywhere in the new code — reviewers must confirm this).
- **Async-test timing:** `_run_job` is a fire-and-forget `create_task`; tests reach `ready` by polling `GET /job/{id}` (`_wait`). Do not assert state synchronously right after `POST /run`.
- **Known leak follow-up:** abandoned jobs are cleaned on the *next* run (Task 3). A job left `ready` forever (tab closed, never chosen) still holds its worktrees until the next run — acceptable for v1, note it in the PR.
