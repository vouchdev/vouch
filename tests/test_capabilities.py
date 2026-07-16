"""Capabilities descriptor — must match the JSONL handler registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import capabilities
from vouch.jsonl_server import HANDLERS


def test_import_apply_is_not_on_agent_surfaces() -> None:
    """kb.import_apply writes past the review gate, so agents must not reach it.

    It stays a human-only CLI command; the read-only kb.import_check survives
    on every surface. (The real fix is gated import — roadmap 8.2.)
    """
    from vouch.jsonl_server import HANDLERS

    assert "kb.import_apply" not in HANDLERS
    assert "kb.import_apply" not in set(capabilities.capabilities().methods)
    # read-only diff stays available to agents
    assert "kb.import_check" in HANDLERS

    from vouch.server import mcp

    assert mcp._tool_manager.get_tool("kb_import_apply") is None
    assert mcp._tool_manager.get_tool("kb_import_check") is not None


def test_capabilities_matches_jsonl_handlers() -> None:
    caps = capabilities.capabilities()
    declared = set(caps.methods)
    implemented = set(HANDLERS.keys())
    assert declared == implemented, (
        f"capabilities/handlers mismatch: "
        f"missing handlers={declared - implemented}, "
        f"missing capabilities={implemented - declared}"
    )


# --- host_compat drift detection (#237) -----------------------------------
#
# vouch declares openclaw.compat.pluginApi in package.json (openclaw.plugin.json
# bans openclaw.* dead dialect fields). The same value must surface in
# kb.capabilities so non-OpenClaw clients can detect compat without parsing
# package.json. These tests fail CI with a clear message if the two
# declarations drift apart.

_PACKAGE_JSON_PATH = (
    Path(__file__).resolve().parent.parent / "package.json"
)


def _package_plugin_api() -> str:
    manifest = json.loads(_PACKAGE_JSON_PATH.read_text(encoding="utf-8"))
    return manifest["openclaw"]["compat"]["pluginApi"]


def test_capabilities_host_compat_matches_openclaw_manifest() -> None:
    """kb.capabilities.host_compat.openclaw.pluginApi must equal the
    pluginApi range declared in package.json. A bump in one file
    without the other is exactly the "host compat drift" #237 asks CI to
    catch."""
    caps = capabilities.capabilities()
    manifest_range = _package_plugin_api()
    capabilities_range = caps.host_compat.get("openclaw", {}).get("pluginApi")
    assert capabilities_range == manifest_range, (
        f"host compat drift: package.json declares pluginApi="
        f"{manifest_range!r} but kb.capabilities.host_compat reports "
        f"{capabilities_range!r}. Keep both in sync."
    )


def test_capabilities_host_compat_present_and_nonempty() -> None:
    """host_compat must not silently degrade to {} when the manifest is
    readable -- that would defeat the drift check above by making both
    sides agree on "missing" rather than catching real drift."""
    caps = capabilities.capabilities()
    assert "openclaw" in caps.host_compat
    assert "pluginApi" in caps.host_compat["openclaw"]
    assert caps.host_compat["openclaw"]["pluginApi"].strip() != ""


def test_load_host_compat_returns_empty_on_missing_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """_load_host_compat must degrade gracefully (empty dict, no raise) if
    package.json is absent -- e.g. installed as a standalone wheel without
    package.json packaged alongside it."""
    monkeypatch.setattr(
        capabilities, "_PACKAGE_JSON_PATH", tmp_path / "does-not-exist.json"
    )
    assert capabilities._load_host_compat() == {}


def test_load_host_compat_returns_empty_on_malformed_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A malformed package.json must not crash capabilities() -- it's
    reporting diagnostic info, not validating the install."""
    bad = tmp_path / "package.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(capabilities, "_PACKAGE_JSON_PATH", bad)
    assert capabilities._load_host_compat() == {}


def test_mcp_tools_match_methods() -> None:
    """Every MCP kb_* tool maps to a capabilities method and vice-versa.

    Closes the MCP half of the 3-surface parity invariant that the JSONL
    check above did not cover. Uses the unfiltered server object (profiles
    apply only in run_stdio).
    """
    from vouch.server import mcp

    tool_names = {n for n in mcp._tool_manager._tools if n.startswith("kb_")}
    as_methods = {"kb." + n.split("_", 1)[1] for n in tool_names}
    declared = set(capabilities.METHODS)
    assert as_methods == declared, (
        f"mcp/methods mismatch: "
        f"missing tools={declared - as_methods}, "
        f"undeclared tools={as_methods - declared}"
    )


# Irregular method → cli-command mirrors. The path part (before any flag) must
# exist in the click tree; flags document which invocation form is the mirror.
_CLI_MIRRORS = {
    "kb.list_pending": "pending",
    "kb.triage_pending": "triage",
    "kb.clear_claims": "claims-clear",
    "kb.volunteer_context": "session volunteer",
    "kb.list_sessions": "session list",
    "kb.session_transcript": "session transcript",
    "kb.summarize_session": "session summarize",
    "kb.register_source": "source add",
    "kb.register_source_from_path": "source add",
    "kb.list_sources": "source list",
    "kb.source_verify": "source verify",
    "kb.session_start": "session start",
    "kb.session_end": "session end",
    "kb.embeddings_stats": "embeddings stats",
    "kb.provenance_rebuild": "provenance rebuild",
    "kb.index_rebuild": "reindex",
    "kb.reindex_embeddings": "reindex --embeddings",
    "kb.dedup_scan": "dedup",
    "kb.eval_embeddings": "eval embedding",
    "kb.graph_export": "graph",
    "kb.propose_theme": "detect-themes --propose",
}


def test_cli_commands_match_methods() -> None:
    """Every kb.* method has a CLI mirror — the third surface of the parity
    invariant (JSONL and MCP are covered above).

    Default rule: kb.foo_bar mirrors as `vouch foo-bar`. Anything irregular is
    declared in _CLI_MIRRORS; a new method with neither fails here until its
    CLI command lands.
    """
    import click

    from vouch.cli import cli as root

    paths: set[str] = set()

    def walk(group: click.Group, prefix: str = "") -> None:
        for name, cmd in group.commands.items():
            full = f"{prefix}{name}"
            paths.add(full)
            if isinstance(cmd, click.Group):
                walk(cmd, full + " ")

    walk(root)

    stale = set(_CLI_MIRRORS) - set(capabilities.METHODS)
    assert not stale, f"_CLI_MIRRORS entries for unknown methods: {stale}"

    missing = []
    for method in capabilities.METHODS:
        target = _CLI_MIRRORS.get(
            method, method.removeprefix("kb.").replace("_", "-")
        )
        cmd_path = target.split(" --")[0].strip()
        if cmd_path not in paths:
            missing.append(f"{method} -> vouch {target}")
    assert not missing, (
        "kb.* methods with no cli mirror:\n" + "\n".join(missing)
    )
