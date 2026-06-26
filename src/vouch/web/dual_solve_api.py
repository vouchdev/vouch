"""dual-solve web runner: routes mounted into the review-ui under an explicit
opt-in flag. The blocking engine work runs in a threadpool; progress streams
over the review-ui's existing websocket. The review gate is preserved -- the
choose step only ever proposes to the kb.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import dual_solve as ds
from ..auto_pr import SubprocessRunner
from ..sandbox import DEFAULT_SANDBOX_IMAGE, DockerAgentRunner, require_docker_sandbox


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


class _ChooseReq(BaseModel):
    job_id: str
    winner: Literal["claude", "codex"] | None = None
    reason: str = ""


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
    sandboxed: bool = False,
    sandbox_image: str | None = None,
) -> None:
    """Mount the dual-solve routes. No-op unless ``enabled``."""
    if not enabled:
        return
    base_runner = SubprocessRunner()
    sandbox_image = sandbox_image or DEFAULT_SANDBOX_IMAGE
    # fail fast at app-build time if we're not in a git repo: dual-solve can't
    # create worktrees otherwise.
    git_root: Path = ds.repo_root(base_runner, store.root)
    app.state.dual_solve_git_root = str(git_root)
    if sandboxed:
        require_docker_sandbox(sandbox_image, runner=base_runner)
    runner = (
        DockerAgentRunner(repo_root=git_root, runner=base_runner, image=sandbox_image)
        if sandboxed else base_runner
    )

    @app.get("/dual-solve", response_class=HTMLResponse, dependencies=guarded)
    def dual_solve_page(request: Request) -> Any:
        return render(request, "dual_solve.html", {"active": "dual-solve"})

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
            # leak-free today: prepare only raises in fetch_issue/ground_prompt,
            # i.e. BEFORE any worktree is created (run_candidate swallows its own
            # errors into cand.error). if prepare ever grows a raising path after
            # worktree creation, those worktrees would orphan here -- clean them.
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
        app.state.dual_solve_task = asyncio.create_task(_run_job(job, loop))
        return {"job_id": job.id}

    @app.get("/dual-solve/job/{job_id}", dependencies=guarded)
    def dual_solve_job(job_id: str) -> dict[str, Any]:
        job = getattr(app.state, "dual_solve_job", None)
        if job is None or job.id != job_id:
            raise HTTPException(404, "no such job")
        return _serialize(job)

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
            await hub.broadcast(_frame(job, "error", str(exc)))
            raise HTTPException(500, f"finalize failed: {exc}") from exc
        job.proposed_ids = ids
        job.kept_branch = chosen.branch if chosen is not None else None
        job.status = "done"
        await hub.broadcast(_frame(job, "done"))
        return {"kept_branch": job.kept_branch, "proposed_ids": ids}
