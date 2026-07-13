"""Tier-2 e2e: the real Codex CLI reads the installed adapter (#389).

The unit tests in test_install_adapter.py assert what the installer writes;
nothing there proves the real codex CLI accepts it — the old T1 silent-skip
behavior (installer no-op whenever `.codex/config.toml` existed) is exactly
the class of bug only a live gate catches. This suite closes that gap,
following the pattern tests/test_openclaw_plugin_load_real.py set: install
the adapter into a temp project at the highest shipped tier, mark the
project trusted inside an isolated ``CODEX_HOME``, and assert through codex
itself (``codex mcp list --json``) that the vouch server is visible — next
to a pre-seeded unrelated server on the merge path, and alone on the
fresh-install path.

Assertions target the observable contract (server listed, config parses,
snippet present), not codex internals, so codex version bumps shouldn't
break the suite. Skips when the ``codex`` CLI is not on PATH (e.g. GitHub
CI). Every codex invocation runs with a throwaway ``CODEX_HOME`` and a temp
project cwd, so the user's real ``~/.codex`` is never touched; listing
configured servers needs no network and no credentials.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from vouch.install_adapter import install

CODEX = shutil.which("codex")

pytestmark = pytest.mark.skipif(CODEX is None, reason="codex CLI not on PATH")


def _run_codex(
    env: dict[str, str], cwd: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    assert CODEX is not None
    return subprocess.run(
        [CODEX, *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _isolated_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "CODEX_HOME": str(home)}


def _trust(home: Path, project: Path) -> None:
    # Codex loads project-scoped .codex/ layers only for trusted projects;
    # this is the non-interactive equivalent of answering the trust prompt.
    # The path is a TOML quoted key, so escape it with json.dumps (TOML
    # basic strings share JSON's escaping) — a backslash or quote in the
    # path would otherwise produce invalid TOML.
    key = json.dumps(str(project))
    with (home / "config.toml").open("a", encoding="utf-8") as fh:
        fh.write(f'[projects.{key}]\ntrust_level = "trusted"\n')


def _mcp_servers(env: dict[str, str], cwd: Path) -> list[dict]:
    result = _run_codex(env, cwd, "mcp", "list", "--json")
    assert result.returncode == 0, (
        f"codex mcp list failed.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # `codex mcp list --json` may emit a one-time notice before the JSON,
    # so try a straight parse first and fall back to slicing from the first
    # bracket; on failure surface stdout/stderr so the reason is actionable.
    try:
        servers = json.loads(result.stdout)
    except json.JSONDecodeError:
        start = result.stdout.find("[")
        if start == -1:
            raise AssertionError(
                f"codex mcp list produced no JSON array.\n"
                f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            ) from None
        try:
            servers = json.loads(result.stdout[start:])
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"codex mcp list JSON did not parse: {e}\n"
                f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
            ) from e
    assert isinstance(servers, list)
    return servers


def test_merge_install_is_visible_to_codex(tmp_path: Path) -> None:
    """The #384 regression at the live level: a project where codex is
    already configured must end up with vouch wired next to the user's
    existing server, as seen by codex itself."""
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text(
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other-server"\n',
        encoding="utf-8",
    )

    install("codex", target=project, tier="T4")

    # the merged file is still valid toml with both servers side by side
    merged = tomllib.loads(
        (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert merged["model"] == "gpt-5"
    assert set(merged["mcp_servers"]) == {"other", "vouch"}

    env = _isolated_env(home)
    _trust(home, project)
    servers = {s["name"]: s for s in _mcp_servers(env, project)}
    assert "vouch" in servers, f"vouch not listed: {sorted(servers)}"
    assert "other" in servers, "user's pre-existing server disappeared"
    transport = servers["vouch"]["transport"]
    assert transport["command"] == "vouch"
    assert transport["args"] == ["serve"]


def test_fresh_install_is_visible_to_codex(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()

    install("codex", target=project, tier="T4")

    env = _isolated_env(home)
    _trust(home, project)
    names = [s["name"] for s in _mcp_servers(env, project)]
    assert names == ["vouch"], names


def test_untrusted_project_config_stays_inert(tmp_path: Path) -> None:
    """Codex ignores project-scoped config for untrusted projects — the
    install must not leak into a session the user never trusted."""
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    install("codex", target=project, tier="T4")

    env = _isolated_env(home)  # note: no _trust() call
    names = [s["name"] for s in _mcp_servers(env, project)]
    assert "vouch" not in names, names


def test_full_tier_artifacts_present(tmp_path: Path) -> None:
    """The T4 install ships every codex-readable surface: merged config,
    fenced AGENTS.md, the nine skills, and the Stop hook."""
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    install("codex", target=project, tier="T4")

    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- BEGIN vouch -->" in agents
    assert "VOUCH_AGENT=codex" in agents.replace("`", "")

    assert (project / ".codex" / "skills" / "vouch-recall" / "SKILL.md").is_file()

    hooks = json.loads(
        (project / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    cmds = [h["command"] for g in hooks["hooks"]["Stop"] for h in g["hooks"]]
    assert "vouch capture ingest-codex --hook" in cmds

    # and codex still accepts the whole tree
    env = _isolated_env(home)
    _trust(home, project)
    assert [s["name"] for s in _mcp_servers(env, project)] == ["vouch"]


def test_nothing_written_outside_project_and_home(tmp_path: Path) -> None:
    """#179 invariant at the live level: after an install plus a codex
    invocation, the throwaway home holds only what we put there and the
    project holds only adapter artifacts — the real ~/.codex is never in
    play because CODEX_HOME is pinned for every invocation."""
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    result = install("codex", target=project, tier="T4")
    for rel in (*result.written, *result.appended, *result.merged):
        assert (project / rel).resolve().is_relative_to(project.resolve())

    env = _isolated_env(home)
    _trust(home, project)
    _mcp_servers(env, project)
    # the trust entry is the only file vouch's test wrote into the home;
    # codex may add its own state (caches, logs) but only under that home.
    assert (home / "config.toml").is_file()
