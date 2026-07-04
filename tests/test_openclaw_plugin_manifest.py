"""openclaw.plugin.json contract checks (#228).

These enforce the *current* OpenClaw plugin dialect: the loader requires
``id`` + ``configSchema``, reads the entry module from package.json's
``openclaw.extensions``, and publishes ``skills`` as directories containing
SKILL.md. The live-loader counterpart is tests/test_openclaw_plugin_load_real.py.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from vouch.openclaw.context_engine import ENGINE_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "openclaw.plugin.json"
PACKAGE_JSON_PATH = REPO_ROOT / "package.json"
EXTENSION_PATH = REPO_ROOT / "adapters" / "openclaw" / "vouch-context-engine.mjs"
SKILL_NAMES = (
    "vouch-propose-from-pr",
    "vouch-recall",
    "vouch-resolve-issue",
    "vouch-status",
)


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def package_json() -> dict:
    return json.loads(PACKAGE_JSON_PATH.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _body_after_frontmatter(text: str) -> str:
    parts = text.split("---", 2)
    assert len(parts) == 3, "expected yaml frontmatter"
    return parts[2].strip()


def test_manifest_required_fields(manifest: dict) -> None:
    # the 2026.6 loader hard-requires exactly these two fields
    assert manifest["id"] == "vouch"
    assert isinstance(manifest["configSchema"], dict)


def test_manifest_id_matches_engine_and_entry(manifest: dict) -> None:
    """Plugin id == engine id == JS entry export id.

    OpenClaw's installer auto-binds plugins.slots.contextEngine to the
    PLUGIN id, and resolveContextEngine looks that value up by ENGINE id;
    the loader separately rejects the import when the entry's export id
    differs from the manifest id. All three must stay identical.
    """
    assert manifest["id"] == ENGINE_ID
    entry_text = EXTENSION_PATH.read_text(encoding="utf-8")
    assert re.search(r"^\s*id: 'vouch',$", entry_text, flags=re.M)
    assert "registerContextEngine" in entry_text


def test_manifest_versions_in_step(manifest: dict, package_json: dict) -> None:
    version = _pyproject_version()
    assert manifest["version"] == version
    assert package_json["version"] == version


def test_manifest_kind_is_context_engine(manifest: dict) -> None:
    assert manifest["kind"] == "context-engine"
    entry_text = EXTENSION_PATH.read_text(encoding="utf-8")
    assert re.search(r"^\s*kind: 'context-engine',$", entry_text, flags=re.M)


def test_package_json_declares_entry_and_compat(package_json: dict) -> None:
    openclaw = package_json["openclaw"]
    extensions = openclaw["extensions"]
    assert extensions, "openclaw.extensions must list the context engine entry"
    entry = REPO_ROOT / extensions[0]
    assert entry.resolve() == EXTENSION_PATH.resolve()
    assert entry.is_file()
    assert openclaw["compat"]["pluginApi"]


def test_manifest_skills_are_publishable_dirs(manifest: dict) -> None:
    """Each skills entry must resolve to dirs OpenClaw can publish.

    The loader symlinks either the listed dir itself (when it holds a
    SKILL.md) or each child dir holding a SKILL.md; a path to a lone .md
    file silently publishes nothing — the old dialect's mistake.
    """
    skills = manifest["skills"]
    assert skills
    published: set[str] = set()
    for rel in skills:
        root = REPO_ROOT / rel
        assert root.is_dir(), f"skills entry must be a directory: {rel}"
        if (root / "SKILL.md").is_file():
            published.add(root.name)
            continue
        for child in root.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                published.add(child.name)
    assert published == set(SKILL_NAMES)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_openclaw_skills_stay_in_sync_with_claude_commands(name: str) -> None:
    skill = REPO_ROOT / "adapters" / "openclaw" / "skills" / name / "SKILL.md"
    command = (
        REPO_ROOT / "adapters" / "claude-code" / ".claude" / "commands" / f"{name}.md"
    )
    assert _body_after_frontmatter(skill.read_text(encoding="utf-8")) == (
        _body_after_frontmatter(command.read_text(encoding="utf-8"))
    ), f"{name}: openclaw SKILL.md body drifted from the claude-code command"


def test_manifest_carries_no_dead_dialect_fields(manifest: dict) -> None:
    """The pre-2026.6 dialect's fields are silently ignored by the loader.

    Keeping them around would suggest they still do something; they don't.
    """
    dead_fields = (
        "family",
        "mcpServers",
        "shared_deps",
        "excluded_from_install",
        "openclaw",
        "contracts",
    )
    for dead in dead_fields:
        assert dead not in manifest, f"dead manifest field resurrected: {dead}"
