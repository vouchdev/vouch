"""`vouch install-mcp <host>` — idempotent adapter writer (vouchdev/vouch#179).

The writer copies per-host templates from ``adapters/<host>/`` into a target
project. Each host ships an ``install.yaml`` manifest declaring which files
land at which paths under which tier (T1 = MCP wire, T2 = CLAUDE.md fence,
T3 = optional slash commands, T4 = optional settings/hooks). Tiers stack:
``--tier T4`` runs everything; ``--tier T1`` stops after the MCP config.

The writer is idempotent: files that already exist are left alone (the
``skipped`` channel), and CLAUDE.md gets a fenced block appended (the
``appended`` channel) so re-runs don't duplicate the snippet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.cli import cli
from vouch.install_adapter import (
    ADAPTERS_DIR,
    AdapterError,
    InstallResult,
    available_adapters,
    install,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- catalogue ------------------------------------------------------------


def test_available_adapters_lists_at_least_six_hosts() -> None:
    """Issue #179 acceptance: --list must enumerate >=6 hosts."""
    hosts = available_adapters()
    assert len(hosts) >= 6, f"only {len(hosts)} hosts: {hosts}"
    # The flagship surfaces named in the issue must all be present.
    must_have = {"claude-code", "claude-desktop", "cursor", "continue",
                 "windsurf", "cline"}
    missing = must_have - set(hosts)
    assert not missing, f"missing flagship hosts: {sorted(missing)}"


def test_available_adapters_is_sorted_and_unique() -> None:
    hosts = available_adapters()
    assert hosts == sorted(hosts)
    assert len(hosts) == len(set(hosts))


def test_every_adapter_has_a_parseable_manifest() -> None:
    """Surface manifest errors at test time rather than first-install time."""
    import yaml

    for host in available_adapters():
        manifest = ADAPTERS_DIR / host / "install.yaml"
        assert manifest.is_file(), f"{host}: missing install.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{host}: manifest must be a YAML mapping"
        assert data.get("host") == host, f"{host}: manifest host field mismatches dirname"
        assert "tiers" in data, f"{host}: manifest missing tiers"
        assert any(t in data["tiers"] for t in ("T1", "T2", "T3", "T4")), \
            f"{host}: manifest declares zero tiers"


# --- claude-code: the reference adapter (T1..T4 all populated) -----------


def test_install_claude_code_t1_writes_only_mcp_json(tmp_path: Path) -> None:
    result = install("claude-code", target=tmp_path, tier="T1")
    assert (tmp_path / ".mcp.json").is_file()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "commands").exists()
    body = json.loads((tmp_path / ".mcp.json").read_text())
    assert body["mcpServers"]["vouch"]["command"] == "vouch"
    assert result.written == [".mcp.json"]
    assert result.appended == []
    assert result.skipped == []


def test_install_claude_code_t4_writes_all_tiers(tmp_path: Path) -> None:
    result = install("claude-code", target=tmp_path, tier="T4")
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    cmd_dir = tmp_path / ".claude" / "commands"
    assert (cmd_dir / "vouch-recall.md").is_file()
    assert (cmd_dir / "vouch-status.md").is_file()
    assert (cmd_dir / "vouch-resolve-issue.md").is_file()
    assert (cmd_dir / "vouch-propose-from-pr.md").is_file()
    assert (cmd_dir / "vouch-ask.md").is_file()
    assert (cmd_dir / "vouch-remember.md").is_file()
    assert (cmd_dir / "vouch-record.md").is_file()
    assert (cmd_dir / "vouch-followup.md").is_file()
    assert (cmd_dir / "vouch-standup.md").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()
    # T1 .mcp.json + T2 CLAUDE.md + 9 T3 commands + T4 settings = 12 files.
    assert len(result.written) == 12, result.written


def test_install_claude_code_is_idempotent(tmp_path: Path) -> None:
    install("claude-code", target=tmp_path, tier="T4")
    second = install("claude-code", target=tmp_path, tier="T4")
    assert second.written == []
    # CLAUDE.md was created as a verbatim fenced copy on first install; the
    # second install sees the fence and skips, NOT appends, so re-runs of
    # `install-mcp` stay flat-noop on a previously-installed tree.
    assert set(second.skipped) == {
        ".mcp.json",
        "CLAUDE.md",
        ".claude/commands/vouch-recall.md",
        ".claude/commands/vouch-status.md",
        ".claude/commands/vouch-resolve-issue.md",
        ".claude/commands/vouch-propose-from-pr.md",
        ".claude/commands/vouch-ask.md",
        ".claude/commands/vouch-remember.md",
        ".claude/commands/vouch-record.md",
        ".claude/commands/vouch-followup.md",
        ".claude/commands/vouch-standup.md",
        ".claude/settings.json",
    }


def test_settings_json_merges_into_existing(tmp_path: Path) -> None:
    """User already has .claude/settings.json — vouch merges its hooks and
    permission allowlist in without clobbering the user's content."""
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {
            "SessionStart": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "my-own-hook"}]}
            ]
        },
    }))
    result = install("claude-code", target=tmp_path, tier="T4")
    merged = json.loads((settings_dir / "settings.json").read_text())

    # user content preserved
    assert "Bash(ls:*)" in merged["permissions"]["allow"]
    start_cmds = [h["command"] for g in merged["hooks"]["SessionStart"] for h in g["hooks"]]
    assert "my-own-hook" in start_cmds

    # vouch content merged in
    assert "mcp__vouch__kb_status" in merged["permissions"]["allow"]
    assert any("capture banner" in c for c in start_cmds)
    post = [h["command"] for g in merged["hooks"].get("PostToolUse", []) for h in g["hooks"]]
    end = [h["command"] for g in merged["hooks"].get("SessionEnd", []) for h in g["hooks"]]
    assert any("capture observe" in c for c in post)
    assert any("capture finalize" in c for c in end)

    assert ".claude/settings.json" in result.merged
    assert ".claude/settings.json" not in result.skipped
    assert ".claude/settings.json" not in result.written


def test_settings_json_merge_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    install("claude-code", target=tmp_path, tier="T4")
    first = (tmp_path / ".claude" / "settings.json").read_text()
    second = install("claude-code", target=tmp_path, tier="T4")
    after = (tmp_path / ".claude" / "settings.json").read_text()

    assert first == after  # no change on re-run
    assert ".claude/settings.json" in second.skipped
    assert ".claude/settings.json" not in second.merged

    data = json.loads(after)
    observe_cmds = [
        h["command"]
        for g in data["hooks"]["PostToolUse"]
        for h in g["hooks"]
        if "capture observe" in h["command"]
    ]
    assert len(observe_cmds) == 1  # not duplicated


def test_settings_json_written_fresh_when_absent(tmp_path: Path) -> None:
    result = install("claude-code", target=tmp_path, tier="T4")
    assert ".claude/settings.json" in result.written
    assert ".claude/settings.json" not in result.merged


def test_settings_json_malformed_existing_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{ not valid json ")
    before = (tmp_path / ".claude" / "settings.json").read_text()
    result = install("claude-code", target=tmp_path, tier="T4")
    # unreadable user file is left untouched, not clobbered
    assert (tmp_path / ".claude" / "settings.json").read_text() == before
    assert ".claude/settings.json" in result.skipped
    assert ".claude/settings.json" not in result.merged


def test_settings_json_non_object_existing_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("[1, 2, 3]")
    result = install("claude-code", target=tmp_path, tier="T4")
    assert ".claude/settings.json" in result.skipped


def test_merge_settings_coerces_non_dict_fields() -> None:
    from vouch.install_adapter import _merge_settings

    dst = {"permissions": "oops", "hooks": "also-oops"}
    src = {
        "permissions": {"allow": ["mcp__vouch__kb_status"]},
        "hooks": {"PostToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "vouch capture observe"}]}
        ]},
    }
    changed = _merge_settings(src, dst)
    assert changed is True
    assert "mcp__vouch__kb_status" in dst["permissions"]["allow"]
    cmds = [h["command"] for g in dst["hooks"]["PostToolUse"] for h in g["hooks"]]
    assert "vouch capture observe" in cmds


def test_merge_settings_ignores_malformed_src_groups() -> None:
    from vouch.install_adapter import _merge_settings

    dst: dict = {}
    # a non-list event value and a non-dict group are both skipped defensively
    src = {"hooks": {"BadEvent": "not-a-list", "PostToolUse": ["not-a-dict"]}}
    assert _merge_settings(src, dst) is False  # nothing addable → no change


def test_merge_settings_new_matcher_group_when_none_matches() -> None:
    from vouch.install_adapter import _merge_settings

    dst = {"hooks": {"PostToolUse": [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "user-hook"}]}
    ]}}
    src = {"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "vouch capture observe"}]}
    ]}}
    assert _merge_settings(src, dst) is True
    matchers = [g.get("matcher") for g in dst["hooks"]["PostToolUse"]]
    assert "Edit" in matchers and "*" in matchers  # user group kept, ours added


def test_install_claude_md_appends_when_existing_unfenced(tmp_path: Path) -> None:
    """User has their own CLAUDE.md — our snippet appends inside a fence so
    their content is untouched and we can detect ourselves on re-install."""
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nExisting content.\n")
    result = install("claude-code", target=tmp_path, tier="T2")
    final = (tmp_path / "CLAUDE.md").read_text()
    assert "Existing content." in final
    assert "<!-- BEGIN vouch -->" in final
    assert "<!-- END vouch -->" in final
    assert result.appended == ["CLAUDE.md"]
    assert "CLAUDE.md" not in result.written
    assert "CLAUDE.md" not in result.skipped


def test_install_claude_md_skips_when_already_fenced(tmp_path: Path) -> None:
    """Second install on the same tree must not duplicate the fence."""
    install("claude-code", target=tmp_path, tier="T2")
    before = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    again = install("claude-code", target=tmp_path, tier="T2")
    after = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert before == after
    assert "CLAUDE.md" in again.skipped
    assert "CLAUDE.md" not in again.written
    assert "CLAUDE.md" not in again.appended


# --- openclaw: second adapter with all four tiers, T3 reused from
# claude-code rather than duplicated (vouchdev/vouch#230) --------------------


def test_install_openclaw_t4_writes_all_tiers(tmp_path: Path) -> None:
    result = install("openclaw", target=tmp_path, tier="T4")
    assert (tmp_path / ".openclaw" / "plugins.json").is_file()
    assert (tmp_path / "AGENTS.md").is_file()
    cmd_dir = tmp_path / ".claude" / "commands"
    assert (cmd_dir / "vouch-recall.md").is_file()
    assert (cmd_dir / "vouch-status.md").is_file()
    assert (cmd_dir / "vouch-resolve-issue.md").is_file()
    assert (cmd_dir / "vouch-propose-from-pr.md").is_file()
    assert (tmp_path / ".openclaw" / "policy.json").is_file()
    # T1 plugins.json + T2 AGENTS.md + 4 T3 commands + T4 policy.json = 7.
    assert len(result.written) == 7, result.written


def test_install_openclaw_t3_commands_match_claude_code(tmp_path: Path) -> None:
    """T3 is declared as a reuse of claude-code's commands, not a fork --
    the manifest's `src` points at adapters/claude-code/ directly."""
    install("openclaw", target=tmp_path, tier="T3")
    for name in (
        "vouch-recall.md", "vouch-status.md",
        "vouch-resolve-issue.md", "vouch-propose-from-pr.md",
    ):
        installed = (tmp_path / ".claude" / "commands" / name).read_text(encoding="utf-8")
        ref_path = REPO_ROOT / "adapters" / "claude-code" / ".claude" / "commands" / name
        assert installed == ref_path.read_text(encoding="utf-8")


def test_install_openclaw_is_idempotent(tmp_path: Path) -> None:
    install("openclaw", target=tmp_path, tier="T4")
    second = install("openclaw", target=tmp_path, tier="T4")
    assert second.written == []
    assert set(second.skipped) == {
        ".openclaw/plugins.json",
        "AGENTS.md",
        ".claude/commands/vouch-recall.md",
        ".claude/commands/vouch-status.md",
        ".claude/commands/vouch-resolve-issue.md",
        ".claude/commands/vouch-propose-from-pr.md",
        ".openclaw/policy.json",
    }


# --- error paths ----------------------------------------------------------


def test_install_unknown_adapter_raises() -> None:
    with pytest.raises(AdapterError, match="unknown adapter"):
        install("not-a-real-host", target=Path("/tmp"), tier="T1")


def test_install_unknown_tier_raises(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="unknown tier"):
        install("claude-code", target=tmp_path, tier="T9")


# --- the non-claude-code hosts ship at least T1 ---------------------------


@pytest.mark.parametrize("host", [
    "cursor", "continue", "codex", "claude-desktop",
    "windsurf", "cline", "zed", "openclaw",
])
def test_each_host_writes_its_t1_file(host: str, tmp_path: Path) -> None:
    """Smoke test: every shipped host must produce at least one file at T1."""
    if host not in available_adapters():
        pytest.skip(f"{host} not shipped — see test_available_adapters_lists_at_least_six_hosts")
    result = install(host, target=tmp_path, tier="T1")
    assert result.written or result.appended, (
        f"{host} T1 produced no files; written={result.written} "
        f"appended={result.appended} skipped={result.skipped}"
    )


def test_install_returns_dataclass_with_three_channels(tmp_path: Path) -> None:
    r = install("claude-code", target=tmp_path, tier="T1")
    assert isinstance(r, InstallResult)
    assert isinstance(r.written, list)
    assert isinstance(r.appended, list)
    assert isinstance(r.skipped, list)


# --- CLI surface ----------------------------------------------------------


def test_cli_install_mcp_list_enumerates_hosts() -> None:
    result = CliRunner().invoke(cli, ["install-mcp", "--list"])
    assert result.exit_code == 0, result.output
    for host in ("claude-code", "claude-desktop", "cursor", "continue",
                 "windsurf", "cline"):
        assert host in result.output, f"{host} missing from --list output"


def test_cli_install_mcp_claude_code_writes_into_path(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, [
        "install-mcp", "claude-code", "--path", str(tmp_path), "--tier", "T1",
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".mcp.json").is_file()
    assert ".mcp.json" in result.output


def test_cli_install_mcp_unknown_host_is_clean_error() -> None:
    result = CliRunner().invoke(cli, [
        "install-mcp", "not-a-host", "--path", "/tmp", "--tier", "T1",
    ])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Error:" in result.output


def test_cli_install_mcp_second_run_shows_skipped(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["install-mcp", "claude-code", "--path", str(tmp_path), "--tier", "T1"]
    runner.invoke(cli, args)
    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output
    assert ".mcp.json" in second.output
    # Some marker indicates a skip — the exact glyph is an implementation choice
    # but the second invocation MUST not pretend it wrote anew.
    assert any(s in second.output.lower() for s in ("skipped", "already", "·"))


def test_cli_install_mcp_default_tier_is_t4(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["install-mcp", "claude-code", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    # T4 implies all artefacts present, not just .mcp.json.
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()


# --- packaging --------------------------------------------------------------


def test_wheel_ships_adapters(tmp_path: Path) -> None:
    """1.1.0 regression: wheels shipped without adapters/, so every pip/pipx
    install failed ``vouch install-mcp <host>`` with ``(available: (none))``
    — the templates only existed in source checkouts. The wheel build must
    force-include them at ``vouch/adapters/``.
    """
    pytest.importorskip("hatchling")
    proc = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted(tmp_path.glob("*.whl"))
    assert wheels, proc.stdout
    with zipfile.ZipFile(wheels[-1]) as zf:
        names = zf.namelist()
    assert any(
        n.endswith("vouch/adapters/claude-code/install.yaml") for n in names
    ), "adapter templates missing from the wheel"


def test_installed_wheel_resolves_adapters(tmp_path: Path) -> None:
    """Follow-up to the wheel-contents test, which was not enough: 1.1.0's
    ADAPTERS_DIR pointed three parents above the package, so an installed
    wheel could *contain* the templates yet still report
    "(available: (none))". Import the wheel's own copy of the package (no
    repo checkout in sight) and assert the resolver finds the packaged
    templates.
    """
    pytest.importorskip("hatchling")
    proc = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    wheel = sorted(tmp_path.glob("*.whl"))[-1]
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(site)
    env = dict(os.environ)
    # The unpacked wheel must shadow the repo checkout; deps still resolve
    # from the test venv further down sys.path.
    env["PYTHONPATH"] = str(site)
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import vouch.install_adapter as ia; import json, sys; "
            "print(json.dumps({'file': ia.__file__, "
            "'hosts': ia.available_adapters()}))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    result = json.loads(probe.stdout)
    assert str(site) in result["file"], result  # really the wheel's copy
    assert "claude-code" in result["hosts"], (
        f"installed copy can't resolve adapters: {result}"
    )
