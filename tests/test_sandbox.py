from __future__ import annotations

import os
from pathlib import Path

import pytest

from vouch import auto_pr as ap
from vouch import sandbox


class FakeRunner:
    def __init__(self, result: ap.RunResult | None = None):
        self.result = result or ap.RunResult(0, "", "")
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> ap.RunResult:
        self.calls.append(argv)
        return self.result


def test_docker_agent_runner_passes_non_agent_commands_through(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    fr = FakeRunner()
    runner = sandbox.DockerAgentRunner(repo_root=repo, runner=fr, host_home=tmp_path)
    try:
        runner.run(["git", "status"], cwd=str(repo))
    finally:
        runner.close()

    assert fr.calls == [["git", "status"]]


def test_docker_agent_runner_wraps_agent_with_worktree_and_home_mounts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    cred = home / ".codex" / "auth.json"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    cred.parent.mkdir(parents=True)
    cred.write_text('{"OPENAI_API_KEY":"sk-test"}')

    fr = FakeRunner()
    runner = sandbox.DockerAgentRunner(
        repo_root=repo, runner=fr, image="agent-img", host_home=home)
    try:
        runner.run(["codex", "exec", "fix"], cwd=str(worktree))
        argv = fr.calls[0]
        assert argv[:3] == ["docker", "run", "--rm"]
        assert "--entrypoint" in argv and "" in argv
        assert "--user" in argv
        assert f"{os.getuid()}:{os.getgid()}" in argv
        assert "-w" in argv and str(worktree.resolve()) in argv
        assert "-e" in argv
        assert f"HOME={sandbox.CONTAINER_HOME}" in argv
        assert "-v" in argv
        assert f"{worktree.resolve()}:{worktree.resolve()}" in argv
        assert f"{git_dir.resolve()}:{git_dir.resolve()}" in argv
        assert f"{runner.sandbox_home}:{sandbox.CONTAINER_HOME}" in argv
        assert (runner.sandbox_home / ".codex" / "auth.json").read_text() == (
            '{"OPENAI_API_KEY":"sk-test"}'
        )
        assert "agent-img" in argv
        assert argv[-3:] == ["codex", "exec", "fix"]
    finally:
        runner.close()


def test_require_docker_sandbox_reports_missing_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    fr = FakeRunner(ap.RunResult(1, "", "no such image"))

    with pytest.raises(RuntimeError, match="sandbox image"):
        sandbox.require_docker_sandbox("missing:latest", runner=fr)

    assert fr.calls == [["docker", "image", "inspect", "missing:latest"]]


def test_require_docker_sandbox_reports_missing_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="docker on PATH"):
        sandbox.require_docker_sandbox("agent-img", runner=FakeRunner())
