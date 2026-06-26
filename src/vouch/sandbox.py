"""Docker-backed agent execution for vouch workflows.

This is deliberately a narrow runner wrapper: git / gh stay on the host, while
Claude Code and Codex run inside a Docker image with only the candidate worktree
and a copied agent-auth home mounted in. The KB review gate is outside this
module and remains unchanged.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

from .auto_pr import Runner, RunResult, SubprocessRunner

DEFAULT_SANDBOX_IMAGE = "vouch/coder:latest"
CONTAINER_HOME = "/home/vouch"
AGENT_BINARIES = {"claude", "codex"}
AGENT_CONFIG_PATHS = (
    ".claude.json.api",
    ".claude.json",
    ".claude/.credentials.json",
    ".claude-oauth-credentials.json",
    ".codex/auth.json",
)
PASSTHROUGH_ENV = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
)


def require_docker_sandbox(image: str = DEFAULT_SANDBOX_IMAGE,
                           runner: Runner | None = None) -> None:
    """Fail fast with a clean error when Docker or the sandbox image is missing."""
    if shutil.which("docker") is None:
        raise RuntimeError("sandbox mode requires docker on PATH")
    runner = runner or SubprocessRunner()
    res = runner.run(["docker", "image", "inspect", image])
    if res.code != 0:
        detail = res.stderr.strip() or res.stdout.strip()
        hint = (
            f"sandbox image {image!r} is not available. Build vouch's coder "
            "preset first, or pass --sandbox-image with an image that contains "
            "claude and codex."
        )
        if detail:
            hint += f" docker said: {detail[:200]}"
        raise RuntimeError(hint)


class DockerAgentRunner:
    """Run agent CLIs inside Docker, leaving ordinary commands on the host.

    The mounted home is a temporary copy of known Claude/Codex credential files,
    so agents can authenticate without being able to rewrite the host's real
    config. The candidate worktree is mounted at the same absolute path so
    existing `--cd` / cwd arguments continue to work. The sandbox image is used
    as the runtime, but its entrypoint is bypassed for these one-shot commands
    so host bind mounts keep the caller's UID/GID ownership.
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        runner: Runner | None = None,
        image: str = DEFAULT_SANDBOX_IMAGE,
        host_home: Path | None = None,
        container_home: str = CONTAINER_HOME,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.runner = runner or SubprocessRunner()
        self.image = image
        self.host_home = (host_home or Path.home()).resolve()
        self.container_home = container_home
        self._tmp_home = tempfile.TemporaryDirectory(prefix="vouch-agent-home-")
        self.sandbox_home = Path(self._tmp_home.name)
        self._copy_agent_config()
        atexit.register(self.close)

    def close(self) -> None:
        self._tmp_home.cleanup()

    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> RunResult:
        if not argv or argv[0] not in AGENT_BINARIES:
            return self.runner.run(argv, cwd=cwd, stdin=stdin, timeout=timeout)
        return self.runner.run(
            self._docker_argv(argv, cwd),
            stdin=stdin,
            timeout=timeout,
        )

    def _copy_agent_config(self) -> None:
        for rel in AGENT_CONFIG_PATHS:
            src = self.host_home / rel
            if not src.is_file():
                continue
            dst = self.sandbox_home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def _docker_argv(self, agent_argv: list[str], cwd: str | None) -> list[str]:
        workdir = Path(cwd).resolve() if cwd is not None else self.repo_root
        args = [
            "docker", "run", "--rm", "-i",
            "--entrypoint", "",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-w", str(workdir),
            "-e", f"HOME={self.container_home}",
            "-v", f"{self.sandbox_home}:{self.container_home}",
            "-v", f"{workdir}:{workdir}",
        ]
        git_dir = self.repo_root / ".git"
        if git_dir.exists() and git_dir != workdir:
            args += ["-v", f"{git_dir}:{git_dir}"]
        for key in PASSTHROUGH_ENV:
            if os.environ.get(key):
                args += ["-e", key]
        return [*args, self.image, *agent_argv]
