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
    assert (tmp_path / ".claude" / "settings.json").is_file()
    # T1 .mcp.json + T2 CLAUDE.md + 4 T3 commands + T4 settings = 7 files.
    assert len(result.written) == 7, result.written


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
        ".claude/settings.json",
    }


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
    "windsurf", "cline", "zed",
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
