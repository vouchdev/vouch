"""Tier-2 e2e: the real OpenClaw CLI loads the vouch plugin (#228).

The unit tests in test_openclaw_plugin_manifest.py check the manifest against
vouch's own expectations; nothing there proves OpenClaw's actual plugin loader
accepts the repo. This suite closes that gap: it links the repo root into an
isolated OpenClaw profile, forces a runtime import, and asserts the
vouch-context engine registers and the contextEngine slot binding validates.

Skips when the ``openclaw`` CLI is not on PATH (e.g. GitHub CI). Runs against
a throwaway ``$HOME`` so the user's real OpenClaw state is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

OPENCLAW = shutil.which("openclaw")
REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "vouch"
ENGINE_ID = "vouch"

pytestmark = pytest.mark.skipif(
    OPENCLAW is None, reason="openclaw CLI not on PATH"
)


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert OPENCLAW is not None
    return subprocess.run(
        [OPENCLAW, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="module")
def oc_env() -> Iterator[dict[str, str]]:
    home = tempfile.mkdtemp(prefix="vouch-openclaw-e2e-")
    env = {**os.environ, "HOME": home}
    try:
        yield env
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture(scope="module")
def installed(oc_env: dict[str, str]) -> dict[str, str]:
    result = _run(oc_env, "plugins", "install", "--link", str(REPO_ROOT))
    assert result.returncode == 0, (
        f"openclaw plugins install failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return oc_env


def _inspect(env: dict[str, str]) -> dict:
    result = _run(env, "plugins", "inspect", PLUGIN_ID, "--json", "--runtime")
    assert result.returncode == 0, (
        f"plugins inspect failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Defensive: skip any non-JSON preamble (e.g. one-time doctor notices).
    payload = result.stdout[result.stdout.index("{") :]
    return json.loads(payload)["plugin"]


def test_runtime_import_succeeds(installed: dict[str, str]) -> None:
    plugin = _inspect(installed)
    assert plugin["imported"] is True
    assert plugin["status"] == "loaded"
    assert not plugin.get("error")


def test_context_engine_registers(installed: dict[str, str]) -> None:
    plugin = _inspect(installed)
    assert ENGINE_ID in plugin.get("contextEngineIds", [])


def test_metadata_comes_from_entry_module(installed: dict[str, str]) -> None:
    plugin = _inspect(installed)
    assert plugin["id"] == PLUGIN_ID
    assert plugin["name"] == "Vouch Context Engine"


def test_install_auto_binds_context_engine_slot(installed: dict[str, str]) -> None:
    """kind=context-engine makes the installer bind the slot to the PLUGIN id.

    This is why ENGINE_ID must equal the plugin id: resolveContextEngine
    looks this slot value up in the engine registry.
    """
    config_path = Path(installed["HOME"]) / ".openclaw" / "openclaw.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["plugins"]["slots"]["contextEngine"] == ENGINE_ID


def test_context_engine_slot_binding_validates(installed: dict[str, str]) -> None:
    result = _run(
        installed, "config", "set", "plugins.slots.contextEngine", ENGINE_ID
    )
    assert result.returncode == 0, result.stderr
    result = _run(installed, "config", "validate")
    assert result.returncode == 0, (
        f"config validate failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_skills_publish_as_ready(installed: dict[str, str]) -> None:
    result = _run(installed, "skills", "list")
    assert result.returncode == 0, result.stderr
    for name in (
        "vouch-propose-from-pr",
        "vouch-recall",
        "vouch-resolve-issue",
        "vouch-status",
    ):
        line = next(
            (ln for ln in result.stdout.splitlines() if name in ln), None
        )
        assert line is not None, f"skill {name} not listed:\n{result.stdout}"
        assert "ready" in line, f"skill {name} not ready: {line}"


def test_plugins_doctor_reports_no_vouch_errors(installed: dict[str, str]) -> None:
    result = _run(installed, "plugins", "doctor")
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        lowered = line.lower()
        if PLUGIN_ID in lowered:
            assert "error" not in lowered, line
