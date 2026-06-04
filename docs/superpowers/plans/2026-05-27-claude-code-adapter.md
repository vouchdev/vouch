# Claude Code adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a complete Claude Code adoption story under `adapters/claude-code/` (four composable tiers: MCP config, CLAUDE.md, slash commands, settings/hooks) plus a `vouch install-mcp claude-code` CLI helper that writes the missing pieces into a target project, idempotently.

**Architecture:** Files-only adapter (no new runtime code in vouch core) + one CLI writer command. The four tiers stack: T1 alone equals today's behavior; T4 is the fully-integrated review-gated workflow with hooks and read-only auto-allow. The writer command resolves a project root and writes only files that don't already exist.

**Tech Stack:** Python 3.11+, Click, pytest, ruff, mypy. Markdown for templates/slash commands. JSON for `.mcp.json` and `settings.json`.

---

## Scope Check
Single subsystem — the Claude Code adapter and its installer. One plan, one PR.

## File Structure

Files created or modified:

```
adapters/claude-code/
  README.md                              ← MODIFY: tiered adoption guide, vouch-kb install
  CLAUDE.md.snippet                      ← MODIFY (light edits): keep, normalize header
  .mcp.json                              ← CREATE: T1 template
  .claude/
    commands/
      vouch-recall.md                    ← CREATE: T3
      vouch-status.md                    ← CREATE: T3
      vouch-resolve-issue.md             ← CREATE: T3
      vouch-propose-from-pr.md           ← CREATE: T3
    settings.json                        ← CREATE: T4 hooks + read-only auto-allow

src/vouch/install_adapter.py             ← CREATE: pure file-writer (no Click)
src/vouch/cli.py                         ← MODIFY: add `install-mcp` group + claude-code cmd
tests/test_install_adapter.py            ← CREATE: TDD for the writer
CHANGELOG.md                             ← MODIFY: [Unreleased] Added entry
```

Adapter files live under `adapters/claude-code/` and are the *source of truth* templates committed to the vouch repo. The writer command copies them into a user's project under matching paths.

---

## Task 0: Branch setup

**Files:**
- Modify: none

- [ ] **Step 1: Stash the pre-existing storage.py edit** (it predates this work)

```bash
git stash push -m "pre-existing WIP storage.py" src/vouch/storage.py 2>/dev/null || true
```

- [ ] **Step 2: Branch off latest main**

```bash
git fetch origin main
git switch -c feat/claude-code-adapter origin/main
git status --porcelain | grep -v '^??' || echo "(clean)"
```

Expected: working tree clean except `?? .claude/` and other untracked workdir files.

---

## Task 1: T1 — `.mcp.json` template

**Files:**
- Create: `adapters/claude-code/.mcp.json`

- [ ] **Step 1: Write the file** verbatim

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": {
        "PYTHONUTF8": "1",
        "VOUCH_AGENT": "claude-code"
      }
    }
  }
}
```

- [ ] **Step 2: Validate it parses as JSON**

```bash
python3 -c "import json; json.load(open('adapters/claude-code/.mcp.json'))"
```

Expected: no output (success).

- [ ] **Step 3: Commit**

```bash
git add adapters/claude-code/.mcp.json
git commit -m "feat(adapter/claude-code): T1 — .mcp.json template"
```

---

## Task 2: T2 — normalize `CLAUDE.md.snippet`

**Files:**
- Modify: `adapters/claude-code/CLAUDE.md.snippet`

The existing snippet is good content. Only change: rename intent — it's the *template* you append to an existing CLAUDE.md, not a standalone file. Add a one-line `<!-- BEGIN/END vouch -->` fence so the installer can detect prior installs and avoid duplicating.

- [ ] **Step 1: Read the current snippet** to confirm content

```bash
wc -l adapters/claude-code/CLAUDE.md.snippet
```

- [ ] **Step 2: Prepend the fence + appendix-style header**

Open `adapters/claude-code/CLAUDE.md.snippet` and replace the file with:

```markdown
<!-- BEGIN vouch -->
## Vouch — knowledge base

This repo uses **vouch** for durable agent knowledge. The KB lives in
`.vouch/` and is reviewed in PRs like any other code.

### How to remember things

To preserve a fact, decision, or workflow across sessions:

1. Register evidence: `kb_register_source` (or `kb_register_source_from_path`
   for a file).
2. Propose a claim that cites it: `kb_propose_claim`. Every claim MUST cite
   at least one source or evidence id.
3. For richer write-ups, propose pages: `kb_propose_page` with a markdown
   body that references claims.

You **cannot** write durable knowledge directly. Proposals land in
`.vouch/proposed/` and require human approval via `vouch approve`. This is
intentional.

### How to read

- `kb_search` for keyword search.
- `kb_context` to fill a working set for a task ("what does this KB know
  about X?").
- `kb_read_*` for specific ids.

### Lifecycle hygiene

When you find a claim that's wrong or out-of-date:

- If you can replace it with a corrected version, use `kb_supersede` rather
  than proposing a contradicting claim.
- If two existing claims conflict, mark them with `kb_contradict` so the
  human can choose.
- Re-cite a claim you used recently with `kb_confirm` — it bumps
  `last_confirmed_at` so lint doesn't flag it as stale.

### Identity

You are recorded as `proposed_by: claude-code` in the audit log. Everything
you propose is visible to whoever runs `vouch pending`.
<!-- END vouch -->
```

- [ ] **Step 3: Verify fence + 9-section structure**

```bash
grep -c "<!-- BEGIN vouch -->" adapters/claude-code/CLAUDE.md.snippet
grep -c "<!-- END vouch -->" adapters/claude-code/CLAUDE.md.snippet
```

Expected: `1` and `1`.

- [ ] **Step 4: Commit**

```bash
git add adapters/claude-code/CLAUDE.md.snippet
git commit -m "feat(adapter/claude-code): T2 — fence CLAUDE.md snippet for idempotent install"
```

---

## Task 3: T3 — `vouch-recall` slash command

**Files:**
- Create: `adapters/claude-code/.claude/commands/vouch-recall.md`

- [ ] **Step 1: Create the directory and file**

```bash
mkdir -p adapters/claude-code/.claude/commands
```

- [ ] **Step 2: Write `vouch-recall.md`** verbatim

```markdown
---
description: Recall cited knowledge from the vouch KB about the given topic before answering.
---

Before answering, fetch a context pack from vouch:

1. Call `mcp__vouch__kb_context` with `query: "$ARGUMENTS"`, `limit: 8`,
   `require_citations: true`.
2. Read each returned item; quote the cited source id on any claim you reuse.
3. If the pack is empty or quality.ok is false, say so explicitly before
   answering — don't fabricate citations.

Then continue with the user's question, grounded in what the KB returned.
```

- [ ] **Step 3: Commit**

```bash
git add adapters/claude-code/.claude/commands/vouch-recall.md
git commit -m "feat(adapter/claude-code): T3 — /vouch-recall command"
```

---

## Task 4: T3 — `vouch-status` slash command

**Files:**
- Create: `adapters/claude-code/.claude/commands/vouch-status.md`

- [ ] **Step 1: Write the file**

```markdown
---
description: Show vouch KB health — counts, pending proposals, audit/index state.
---

Run this single shell command and show me its raw output:

```bash
vouch status
```

Then call `mcp__vouch__kb_list_pending` and summarise the queue in one line
(how many pending, who proposed each).
```

- [ ] **Step 2: Commit**

```bash
git add adapters/claude-code/.claude/commands/vouch-status.md
git commit -m "feat(adapter/claude-code): T3 — /vouch-status command"
```

---

## Task 5: T3 — `vouch-resolve-issue` slash command

**Files:**
- Create: `adapters/claude-code/.claude/commands/vouch-resolve-issue.md`

- [ ] **Step 1: Write the file**

```markdown
---
description: Resolve a GitHub issue end-to-end with a vouch session bracketing the work.
---

You are resolving the issue at $ARGUMENTS.

Run this loop:

1. `gh issue view $ARGUMENTS` — read the issue.
2. `mcp__vouch__kb_context` with the issue title — surface prior decisions.
3. `mcp__vouch__kb_session_start` with `task: "<issue title> (#<n>)"` and
   note the returned `session_id`.
4. Do the work (read code, propose fix). Each meaningful finding goes in as
   `mcp__vouch__kb_propose_claim` citing the source that justifies it.
5. `mcp__vouch__kb_session_end` with the session id.
6. Tell the user the session id and that `vouch crystallize <session_id>`
   will approve the proposed claims after they review with `vouch pending`.

Do not call `kb_approve`; the human reviews and approves.
```

- [ ] **Step 2: Commit**

```bash
git add adapters/claude-code/.claude/commands/vouch-resolve-issue.md
git commit -m "feat(adapter/claude-code): T3 — /vouch-resolve-issue command"
```

---

## Task 6: T3 — `vouch-propose-from-pr` slash command

**Files:**
- Create: `adapters/claude-code/.claude/commands/vouch-propose-from-pr.md`

- [ ] **Step 1: Write the file**

```markdown
---
description: Capture a merged PR's decision as a proposed, cited vouch claim.
---

You are capturing the durable decision from the PR at $ARGUMENTS.

1. `gh pr view $ARGUMENTS --json title,body,mergedAt,mergeCommit` — read it.
   If `mergedAt` is null, stop and tell the user the PR isn't merged yet.
2. `mcp__vouch__kb_register_source` with the PR URL as `locator` and the
   PR title as `title`. Note the returned source id.
3. Draft one sentence that captures the decision the PR establishes — the
   *why* future agents need to remember, not a summary of the diff.
4. `mcp__vouch__kb_propose_claim` with that sentence, citing the source id
   from step 2. Type `decision`.
5. Tell the user the proposal id; they review with `vouch show <id>` and
   approve with `vouch approve <id>`.

Never approve your own proposal. The review gate is the point.
```

- [ ] **Step 2: Commit**

```bash
git add adapters/claude-code/.claude/commands/vouch-propose-from-pr.md
git commit -m "feat(adapter/claude-code): T3 — /vouch-propose-from-pr command"
```

---

## Task 7: T4 — `settings.json` (hooks + auto-allow)

**Files:**
- Create: `adapters/claude-code/.claude/settings.json`

- [ ] **Step 1: Write the file**

```json
{
  "permissions": {
    "alwaysAllow": [
      "mcp__vouch__kb_status",
      "mcp__vouch__kb_search",
      "mcp__vouch__kb_context",
      "mcp__vouch__kb_read_claim",
      "mcp__vouch__kb_read_page",
      "mcp__vouch__kb_read_entity",
      "mcp__vouch__kb_read_relation",
      "mcp__vouch__kb_list_claims",
      "mcp__vouch__kb_list_pages",
      "mcp__vouch__kb_list_entities",
      "mcp__vouch__kb_list_relations",
      "mcp__vouch__kb_list_sources",
      "mcp__vouch__kb_list_pending",
      "mcp__vouch__kb_capabilities"
    ]
  },
  "hooks": {
    "SessionStart": [
      {
        "command": "vouch status 2>/dev/null || true",
        "comment": "Show KB counts + pending proposals at the start of every session."
      }
    ]
  }
}
```

- [ ] **Step 2: Validate JSON parses**

```bash
python3 -c "import json; json.load(open('adapters/claude-code/.claude/settings.json'))"
```

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add adapters/claude-code/.claude/settings.json
git commit -m "feat(adapter/claude-code): T4 — settings.json (SessionStart hook + read-only auto-allow)"
```

---

## Task 8: Adapter README — tiered adoption guide

**Files:**
- Modify: `adapters/claude-code/README.md`

- [ ] **Step 1: Replace the file** with the tiered guide

```markdown
# Claude Code adapter

Wires [vouch][v] (an MCP server) into [Claude Code][cc] in four composable
tiers. Stop at any tier — each one works on its own.

[v]: https://github.com/vouchdev/vouch
[cc]: https://claude.com/claude-code

## Prerequisite

```bash
pipx install vouch-kb   # the command is `vouch`
vouch init              # create .vouch/ in your project
```

## The four tiers

| Tier | File | What it does |
|---|---|---|
| T1 | `.mcp.json` | Registers the `vouch` MCP server so the agent has the `kb_*` tools. |
| T2 | `CLAUDE.md` (append the snippet) | Teaches the agent the review-gate workflow. |
| T3 | `.claude/commands/vouch-*.md` | Four slash commands: `/vouch-recall`, `/vouch-status`, `/vouch-resolve-issue`, `/vouch-propose-from-pr`. |
| T4 | `.claude/settings.json` | `SessionStart` hook prints `vouch status`; reads/`list_*` are auto-allowed (writes still prompt). |

## One-shot install

```bash
vouch install-mcp claude-code           # writes T1..T4 into the current project
vouch install-mcp claude-code --tier T2 # stop at T2 (skip slash commands + settings)
```

The command is idempotent — files that already exist are left alone (verbose
mode lists them).

## Manual install (if you prefer)

1. Copy `adapters/claude-code/.mcp.json` to your project root.
2. Append `adapters/claude-code/CLAUDE.md.snippet` to your project's
   `CLAUDE.md` (or `AGENTS.md`). The snippet is fenced with
   `<!-- BEGIN vouch -->` / `<!-- END vouch -->` so it's safe to re-append.
3. Copy `adapters/claude-code/.claude/commands/*.md` to
   `.claude/commands/` in your project.
4. Merge `adapters/claude-code/.claude/settings.json` into your project's
   `.claude/settings.json` (or commit it as-is if you have none).

## Verify

```bash
vouch status                  # KB present?
claude --debug-mcp 2>&1 | grep vouch   # MCP server visible to Claude Code?
```

In a fresh Claude session, ask "what knowledge-base tools do you have?" —
it should enumerate `kb_search`, `kb_propose_claim`, etc.

## Notes

- `VOUCH_AGENT=claude-code` is the default in the bundled `.mcp.json`;
  change it if you run multiple Claude Code seats against the same KB.
- The auto-allow rules in T4 only cover read-only `kb_*` methods — writes
  (`kb_approve`, `kb_reject`, `kb_crystallize`, `kb_propose_*`) still
  prompt, protecting the review gate.
- For hosts that launch from a default cwd (e.g. Claude Desktop), set
  `VOUCH_KB_PATH=/abs/path/.vouch` in the `env` block of `.mcp.json`.
```

- [ ] **Step 2: Commit**

```bash
git add adapters/claude-code/README.md
git commit -m "docs(adapter/claude-code): tiered adoption guide + install-mcp reference"
```

---

## Task 9: Writer module — TDD (test-first)

**Files:**
- Create: `tests/test_install_adapter.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the Claude Code adapter installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch.install_adapter import (
    AdapterError,
    available_adapters,
    install,
)

ADAPTER_ROOT = Path(__file__).resolve().parent.parent / "adapters" / "claude-code"


def test_available_adapters_lists_claude_code() -> None:
    assert "claude-code" in available_adapters()


def test_install_t1_writes_only_mcp_json(tmp_path: Path) -> None:
    result = install("claude-code", target=tmp_path, tier="T1")
    assert (tmp_path / ".mcp.json").is_file()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".claude" / "commands").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    body = json.loads((tmp_path / ".mcp.json").read_text())
    assert body["mcpServers"]["vouch"]["command"] == "vouch"
    assert sorted(result.written) == [".mcp.json"]


def test_install_t4_writes_all_tiers(tmp_path: Path) -> None:
    result = install("claude-code", target=tmp_path, tier="T4")
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()
    cmds = tmp_path / ".claude" / "commands"
    assert (cmds / "vouch-recall.md").is_file()
    assert (cmds / "vouch-status.md").is_file()
    assert (cmds / "vouch-resolve-issue.md").is_file()
    assert (cmds / "vouch-propose-from-pr.md").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert len(result.written) == 7  # mcp + claude.md + 4 commands + settings


def test_install_is_idempotent(tmp_path: Path) -> None:
    install("claude-code", target=tmp_path, tier="T4")
    second = install("claude-code", target=tmp_path, tier="T4")
    assert second.written == []
    assert sorted(second.skipped) == sorted([
        ".mcp.json",
        "CLAUDE.md",
        ".claude/commands/vouch-recall.md",
        ".claude/commands/vouch-status.md",
        ".claude/commands/vouch-resolve-issue.md",
        ".claude/commands/vouch-propose-from-pr.md",
        ".claude/settings.json",
    ])


def test_install_claude_md_appends_when_existing(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nExisting content.\n")
    result = install("claude-code", target=tmp_path, tier="T2")
    final = (tmp_path / "CLAUDE.md").read_text()
    assert "Existing content." in final
    assert "<!-- BEGIN vouch -->" in final
    assert "<!-- END vouch -->" in final
    assert "CLAUDE.md" in result.appended


def test_install_claude_md_skips_when_already_fenced(tmp_path: Path) -> None:
    snippet = (ADAPTER_ROOT / "CLAUDE.md.snippet").read_text()
    (tmp_path / "CLAUDE.md").write_text("# Existing\n\n" + snippet)
    result = install("claude-code", target=tmp_path, tier="T2")
    assert "CLAUDE.md" in result.skipped
    assert "CLAUDE.md" not in result.appended


def test_install_unknown_adapter_raises() -> None:
    with pytest.raises(AdapterError, match="unknown adapter"):
        install("bogus", target=Path("/tmp"), tier="T1")


def test_install_unknown_tier_raises(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="unknown tier"):
        install("claude-code", target=tmp_path, tier="T9")
```

- [ ] **Step 2: Run to confirm RED**

```bash
.venv/bin/python -m pytest tests/test_install_adapter.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'vouch.install_adapter'`.

---

## Task 10: Writer module — GREEN

**Files:**
- Create: `src/vouch/install_adapter.py`

- [ ] **Step 1: Write the minimal implementation**

```python
"""Idempotently install a host adapter (e.g. Claude Code) into a target project.

The adapter templates live under `adapters/<name>/` in the vouch repo. The
installer copies them into `target` paths, skipping files that already exist
verbatim and appending fenced blocks into existing files (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent.parent.parent / "adapters"

_TIER_FILES: dict[str, list[tuple[str, str]]] = {
    # (adapter-relative source, target-relative dest)
    "T1": [(".mcp.json", ".mcp.json")],
    "T2": [("CLAUDE.md.snippet", "CLAUDE.md")],
    "T3": [
        (".claude/commands/vouch-recall.md", ".claude/commands/vouch-recall.md"),
        (".claude/commands/vouch-status.md", ".claude/commands/vouch-status.md"),
        (".claude/commands/vouch-resolve-issue.md", ".claude/commands/vouch-resolve-issue.md"),
        (".claude/commands/vouch-propose-from-pr.md", ".claude/commands/vouch-propose-from-pr.md"),
    ],
    "T4": [(".claude/settings.json", ".claude/settings.json")],
}

_TIER_ORDER = ("T1", "T2", "T3", "T4")
_FENCE_BEGIN = "<!-- BEGIN vouch -->"


class AdapterError(RuntimeError):
    pass


@dataclass
class InstallResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)


def available_adapters() -> list[str]:
    if not ADAPTERS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in ADAPTERS_DIR.iterdir()
        if p.is_dir() and (p / ".mcp.json").is_file()
    )


def install(adapter: str, *, target: Path, tier: str = "T4") -> InstallResult:
    if adapter not in available_adapters():
        raise AdapterError(
            f"unknown adapter {adapter!r} (available: {', '.join(available_adapters())})"
        )
    if tier not in _TIER_ORDER:
        raise AdapterError(
            f"unknown tier {tier!r} (available: {', '.join(_TIER_ORDER)})"
        )

    src_root = ADAPTERS_DIR / adapter
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    result = InstallResult()
    selected = _TIER_ORDER[: _TIER_ORDER.index(tier) + 1]
    for t in selected:
        for src_rel, dst_rel in _TIER_FILES[t]:
            src = src_root / src_rel
            dst = target / dst_rel
            if dst_rel == "CLAUDE.md":
                _install_claude_md(src, dst, result)
            else:
                _install_plain(src, dst, dst_rel, result)
    return result


def _install_plain(src: Path, dst: Path, dst_rel: str, result: InstallResult) -> None:
    if dst.exists():
        result.skipped.append(dst_rel)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    result.written.append(dst_rel)


def _install_claude_md(src: Path, dst: Path, result: InstallResult) -> None:
    snippet = src.read_text(encoding="utf-8")
    if not dst.exists():
        dst.write_text(snippet, encoding="utf-8")
        result.written.append("CLAUDE.md")
        return
    existing = dst.read_text(encoding="utf-8")
    if _FENCE_BEGIN in existing:
        result.skipped.append("CLAUDE.md")
        return
    sep = "" if existing.endswith("\n") else "\n"
    dst.write_text(existing + sep + "\n" + snippet, encoding="utf-8")
    result.appended.append("CLAUDE.md")
```

- [ ] **Step 2: Run tests to confirm GREEN**

```bash
.venv/bin/python -m pytest tests/test_install_adapter.py -q
```

Expected: all 7 tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/vouch/install_adapter.py tests/test_install_adapter.py
git commit -m "feat(install): adapter installer with tier selection + idempotent CLAUDE.md fence"
```

---

## Task 11: CLI wiring — `vouch install-mcp claude-code`

**Files:**
- Modify: `src/vouch/cli.py`

- [ ] **Step 1: Add the import** near the other onboarding-style imports (top of cli.py)

Edit: in the import section, add a line:

```python
from .install_adapter import AdapterError, available_adapters, install as install_adapter
```

- [ ] **Step 2: Add the `install-mcp` command group** at the end of cli.py, before `if __name__ == "__main__":`

```python
# --- install-mcp ----------------------------------------------------------


@cli.group(name="install-mcp")
def install_mcp_group() -> None:
    """Install vouch into a Claude Code-style project (idempotently)."""


@install_mcp_group.command(name="claude-code")
@click.option("--path", default=".", show_default=True,
              type=click.Path(file_okay=False),
              help="Target project root.")
@click.option("--tier", default="T4", show_default=True,
              type=click.Choice(["T1", "T2", "T3", "T4"]),
              help="Stop at the given tier (T1=mcp only; T4=full integration).")
def install_mcp_claude_code(path: str, tier: str) -> None:
    """Drop .mcp.json + CLAUDE.md + slash commands + settings into PATH."""
    target = Path(path).resolve()
    try:
        result = install_adapter("claude-code", target=target, tier=tier)
    except AdapterError as e:
        raise click.ClickException(str(e)) from e
    for f in result.written:
        click.echo(f"  + {f}")
    for f in result.appended:
        click.echo(f"  ~ {f} (appended fenced block)")
    for f in result.skipped:
        click.echo(f"  · {f} (already present)")
    click.echo(
        f"Done — {len(result.written)} written, "
        f"{len(result.appended)} appended, {len(result.skipped)} skipped."
    )
```

- [ ] **Step 3: Run a smoke test against a tmp dir**

```bash
tmp=$(mktemp -d) && .venv/bin/vouch install-mcp claude-code --path "$tmp" --tier T1
ls -1 "$tmp"
rm -rf "$tmp"
```

Expected: prints `+ .mcp.json` and a `Done — 1 written, ...` summary; tmp dir contains `.mcp.json`.

- [ ] **Step 4: Run the full suite + mypy + ruff**

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests
```

Expected: all green; mypy "Success: no issues found"; ruff "All checks passed!".

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py
git commit -m "feat(cli): vouch install-mcp claude-code [--tier T1..T4]"
```

---

## Task 12: CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the `[Unreleased] → Added` entry**

Edit `CHANGELOG.md`. Under `## [Unreleased]`, add (or extend) an `### Added` section:

```markdown
### Added
- Claude Code adapter at `adapters/claude-code/` ships four composable tiers: `.mcp.json` (T1), a fenced `CLAUDE.md` snippet (T2), four slash commands `/vouch-recall`, `/vouch-status`, `/vouch-resolve-issue`, `/vouch-propose-from-pr` (T3), and `.claude/settings.json` with a `SessionStart` hook + read-only `kb_*` auto-allow (T4). New `vouch install-mcp claude-code [--tier T1..T4]` writes them into a project idempotently — existing files are left alone, and `CLAUDE.md` gets a fenced appended block so it's safe to re-run.
```

- [ ] **Step 2: Final verify**

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check src tests
```

Expected: all three green.

- [ ] **Step 3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entry for Claude Code adapter + install-mcp"
GIT_SSH_COMMAND="ssh -i ~/.ssh/plind-junior -o IdentitiesOnly=yes" \
  git push -u origin feat/claude-code-adapter
```

- [ ] **Step 4: Restore the pre-existing storage.py edit on `release/0.1.0`**

```bash
git switch release/0.1.0
git stash pop
git status --porcelain src/vouch/storage.py
```

Expected: ` M src/vouch/storage.py` (the pre-existing edit is back).

---

## Self-Review Checklist (run before handoff)

1. **Spec coverage** — every tier (T1–T4) has its own task; the CLI front door has its own; idempotency is tested.
2. **Placeholder scan** — every code/template step contains the literal file contents; no "TODO / fill in details".
3. **Type consistency** — `install()`, `InstallResult`, `available_adapters()`, `AdapterError` names match across the tests, the implementation, and the CLI.
4. **Idempotency** — `_install_plain` checks `dst.exists()`; `_install_claude_md` checks for the fence; tests cover both.
