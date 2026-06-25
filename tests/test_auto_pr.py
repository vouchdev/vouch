"""tests for vouch auto-pr.

every test runs against a FakeRunner: no network, no real claude/codex/gh.
the subprocess boundary is the only thing mocked; all stage logic is real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vouch import auto_pr as ap


class FakeRunner:
    """matches argv prefixes to canned RunResults and records every call."""

    def __init__(self, script: list[tuple[list[str], ap.RunResult]] | None = None):
        self.script = list(script or [])
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> ap.RunResult:
        self.calls.append(argv)
        for match, result in self.script:
            if argv[: len(match)] == match:
                return result
        return ap.RunResult(0, "", "")


# --- Task 1: effort mapping, data model -----------------------------------

def test_claude_flags_by_effort():
    assert ap.claude_flags("low") == ["--model", "haiku"]
    assert ap.claude_flags("max") == ["--model", "opus"]


def test_codex_flags_by_effort():
    assert ap.codex_flags("medium") == ["-c", "model_reasoning_effort=medium"]
    assert ap.codex_flags("max") == ["-c", "model_reasoning_effort=high"]


def test_flags_reject_bad_effort():
    with pytest.raises(ValueError):
        ap.claude_flags("turbo")
    with pytest.raises(ValueError):
        ap.codex_flags("turbo")


def test_slugify():
    assert ap.slugify("Fix the Thing!") == "fix-the-thing"
    assert ap.slugify("") == "change"
    assert ap.slugify("---") == "change"


def test_parse_repo_variants():
    assert ap.parse_repo("https://github.com/owner/name") == "owner/name"
    assert ap.parse_repo("https://github.com/owner/name.git") == "owner/name"
    assert ap.parse_repo("https://github.com/owner/name/") == "owner/name"
    assert ap.parse_repo("git@github.com:owner/name.git") == "owner/name"
    assert ap.parse_repo("owner/name") == "owner/name"


def test_parse_repo_rejects_garbage():
    with pytest.raises(ValueError):
        ap.parse_repo("not a repo")


# --- Task 2: engine adapter -----------------------------------------------

def test_parse_verdict_approve():
    assert ap.parse_verdict("APPROVE looks good").approved is True


def test_parse_verdict_request_changes():
    v = ap.parse_verdict("REQUEST_CHANGES: missing a test")
    assert v.approved is False
    assert "missing a test" in v.notes


def test_parse_verdict_unknown_first_line_is_reject():
    assert ap.parse_verdict("hmm not sure").approved is False
    assert ap.parse_verdict("").approved is False


def test_engine_text_claude_unwraps_json():
    assert ap.engine_text("claude", '{"result": "APPROVE"}') == "APPROVE"
    assert ap.engine_text("claude", "raw not json") == "raw not json"
    assert ap.engine_text("codex", "APPROVE") == "APPROVE"


def test_engine_fix_builds_claude_argv():
    fr = FakeRunner()
    ap.Engine("claude", "high", fr).fix(cwd="/w", prompt="do it")
    argv = fr.calls[0]
    assert argv[:2] == ["claude", "-p"]
    # safer default: edits auto-accepted, no arbitrary command execution.
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "opus" in argv


def test_engine_full_autonomy_escalates_claude():
    fr = FakeRunner()
    ap.Engine("claude", "high", fr, full_autonomy=True).fix(cwd="/w", prompt="x")
    assert "bypassPermissions" in fr.calls[0]
    # read-only paths never escalate, even under full autonomy.
    fr2 = FakeRunner()
    ap.Engine("claude", "high", fr2, full_autonomy=True).ask(cwd="/w", prompt="x")
    assert "plan" in fr2.calls[0]


def test_engine_fix_builds_codex_argv():
    fr = FakeRunner()
    ap.Engine("codex", "low", fr).fix(cwd="/w", prompt="do it")
    argv = fr.calls[0]
    assert argv[:2] == ["codex", "exec"]
    assert "--sandbox" in argv and "workspace-write" in argv
    assert "model_reasoning_effort=low" in " ".join(argv)


def test_engine_review_returns_verdict():
    fr = FakeRunner([(["claude"], ap.RunResult(0, '{"result": "APPROVE ok"}', ""))])
    v = ap.Engine("claude", "high", fr).review(cwd="/w", diff="diff", prompt="review")
    assert v.approved is True


def test_engine_review_codex_read_only():
    fr = FakeRunner([(["codex"], ap.RunResult(0, "REQUEST_CHANGES: nope", ""))])
    v = ap.Engine("codex", "high", fr).review(cwd="/w", diff="d", prompt="review")
    assert v.approved is False
    assert "--sandbox" in fr.calls[0] and "read-only" in fr.calls[0]


# --- Task 3: workspace resolution -----------------------------------------

def test_resolve_existing_clone_reads_default_branch(tmp_path):
    (tmp_path / ".git").mkdir()
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "symbolic-ref"],
         ap.RunResult(0, "origin/main\n", "")),
    ])
    ctx = ap.resolve_workspace("owner/name", str(tmp_path), fr)
    assert ctx.repo == "owner/name"
    assert ctx.default_branch == "main"
    assert not any(c[:3] == ["gh", "repo", "fork"] for c in fr.calls)


def test_resolve_missing_clone_forks(tmp_path):
    target = tmp_path / "wkdir"
    fr = FakeRunner([
        (["git", "-C", str(target), "symbolic-ref"],
         ap.RunResult(0, "origin/main\n", "")),
    ])
    ap.resolve_workspace("https://github.com/owner/name", str(target), fr,
                         fork_owner="me")
    assert any(c[:3] == ["gh", "repo", "fork"] for c in fr.calls)


def test_resolve_missing_clone_with_push_clones(tmp_path):
    target = tmp_path / "wkdir"
    fr = FakeRunner([
        (["git", "-C", str(target), "symbolic-ref"],
         ap.RunResult(0, "origin/trunk\n", "")),
    ])
    ctx = ap.resolve_workspace("owner/name", str(target), fr, has_push=True)
    assert any(c[:3] == ["gh", "repo", "clone"] for c in fr.calls)
    assert ctx.default_branch == "trunk"


# --- Task 4: guidance detection + bootstrap -------------------------------

def test_find_guidance_picks_up_contributing(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("be nice")
    found = ap.find_guidance(tmp_path)
    assert any(p.name == "CONTRIBUTING.md" for p in found)


def test_bootstrap_writes_skill_when_absent(tmp_path):
    (tmp_path / ".git").mkdir()
    ctx = ap.RepoCtx("o/n", "o/n", tmp_path, "main")
    fr = FakeRunner([
        (["gh", "pr", "list"], ap.RunResult(0, "[]", "")),
        (["claude"], ap.RunResult(0, '{"result": "## contributing\\nrun make check"}', "")),
    ])
    eng = ap.Engine("claude", "high", fr)
    text = ap.detect_or_bootstrap_guidance(ctx, eng, fr)
    skill = tmp_path / ".claude" / "skills" / "auto-pr-contributing" / "SKILL.md"
    assert skill.exists()
    assert "contributing" in text.lower()


def test_bootstrap_skipped_when_present(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "CONTRIBUTING.md").write_text("house rules")
    ctx = ap.RepoCtx("o/n", "o/n", tmp_path, "main")
    fr = FakeRunner()
    eng = ap.Engine("claude", "high", fr)
    text = ap.detect_or_bootstrap_guidance(ctx, eng, fr)
    assert "house rules" in text
    assert not any(c[:2] == ["gh", "pr"] for c in fr.calls)


# --- Task 5: work-item sourcing -------------------------------------------

def test_open_issues_maps_to_workitems():
    ctx = ap.RepoCtx("o/n", "o/n", Path("/w"), "main")
    issues = '[{"number": 7, "title": "Fix bug", "body": "b", "url": "u"}]'
    fr = FakeRunner([(["gh", "issue", "list"], ap.RunResult(0, issues, ""))])
    items = ap.open_issues(ctx, fr)
    assert items[0].kind == "issue" and items[0].number == 7
    assert items[0].slug == "fix-bug"


def test_is_duplicate_true_when_search_hits():
    ctx = ap.RepoCtx("o/n", "o/n", Path("/w"), "main")
    fr = FakeRunner([(["gh", "pr", "list"],
                      ap.RunResult(0, '[{"title": "Fix bug", "url": "x"}]', ""))])
    assert ap.is_duplicate(ctx, "Fix bug", fr) is True


def test_is_duplicate_false_when_no_hit():
    ctx = ap.RepoCtx("o/n", "o/n", Path("/w"), "main")
    fr = FakeRunner([(["gh", "pr", "list"], ap.RunResult(0, "[]", ""))])
    assert ap.is_duplicate(ctx, "totally novel thing", fr) is False


def test_is_duplicate_fails_closed_on_error():
    ctx = ap.RepoCtx("o/n", "o/n", Path("/w"), "main")
    fr = FakeRunner([(["gh", "pr", "list"], ap.RunResult(1, "", "rate limited"))])
    assert ap.is_duplicate(ctx, "anything", fr) is True


def test_source_fills_remainder_with_discovery(monkeypatch):
    ctx = ap.RepoCtx("o/n", "o/n", Path("/w"), "main")
    fr = FakeRunner([
        (["gh", "issue", "list"],
         ap.RunResult(0, '[{"number":1,"title":"a","body":"","url":"u"}]', "")),
        (["gh", "pr", "list"], ap.RunResult(0, "[]", "")),
    ])
    eng = ap.Engine("claude", "high", fr)
    monkeypatch.setattr(
        ap, "discover_items",
        lambda c, e, k: [ap.WorkItem("discovered", "b", "", "b")][:k],
    )
    items = ap.source_work_items(ctx, 2, fr, eng)
    assert len(items) == 2
    assert items[0].kind == "issue" and items[1].kind == "discovered"


# --- Task 6: local gate ----------------------------------------------------

def test_detect_gate_make(tmp_path):
    (tmp_path / "Makefile").write_text("check:\n\techo ok\n")
    assert ap.detect_gate(tmp_path) == ["make", "check"]


def test_detect_gate_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert ap.detect_gate(tmp_path) == ["python", "-m", "pytest", "-q"]


def test_detect_gate_none(tmp_path):
    assert ap.detect_gate(tmp_path) is None


def test_run_gate_no_gate_is_ok(tmp_path):
    ok, log = ap.run_gate(tmp_path, FakeRunner())
    assert ok is True and "no test gate" in log.lower()


def test_run_gate_red_blocks(tmp_path):
    (tmp_path / "Makefile").write_text("check:\n\tfalse\n")
    fr = FakeRunner([(["make", "check"], ap.RunResult(1, "", "boom"))])
    ok, log = ap.run_gate(tmp_path, fr)
    assert ok is False and "boom" in log


# --- Task 7: per-item processing ------------------------------------------

def _ctx(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return ap.RepoCtx("o/n", "o/n", tmp_path, "main")


def test_open_pr_qualifies_head_with_fork_owner(tmp_path):
    ctx = ap.RepoCtx("o/n", "o/n", tmp_path, "main", fork_owner="me")
    fr = FakeRunner([(["gh", "pr", "create"],
                      ap.RunResult(0, "https://github.com/o/n/pull/5", ""))])
    item = ap.WorkItem("issue", "t", "b", "t", number=2)
    url = ap.open_pr(ctx, item, "auto-pr/t", fr)
    assert url == "https://github.com/o/n/pull/5"
    create = next(c for c in fr.calls if c[:3] == ["gh", "pr", "create"])
    assert "me:auto-pr/t" in create
    assert any("push" in c for c in fr.calls if c[:2] == ["git", "-C"])


def test_open_pr_raises_when_create_fails(tmp_path):
    ctx = ap.RepoCtx("o/n", "o/n", tmp_path, "main", fork_owner="me")
    fr = FakeRunner([(["gh", "pr", "create"], ap.RunResult(1, "", "denied"))])
    item = ap.WorkItem("issue", "t", "b", "t")
    with pytest.raises(RuntimeError):
        ap.open_pr(ctx, item, "auto-pr/t", fr)


def test_resolve_detects_fork_owner_on_fork(tmp_path):
    target = tmp_path / "wk"  # missing clone -> resolve forks it
    fr = FakeRunner([
        (["git", "-C", str(target), "symbolic-ref"],
         ap.RunResult(0, "origin/main\n", "")),
        (["gh", "api", "user"], ap.RunResult(0, "octocat\n", "")),
    ])
    ctx = ap.resolve_workspace("owner/name", str(target), fr)
    assert ctx.fork_owner == "octocat"


def test_resolve_existing_clone_does_not_autodetect_fork_owner(tmp_path):
    (tmp_path / ".git").mkdir()
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "symbolic-ref"],
         ap.RunResult(0, "origin/main\n", "")),
    ])
    ctx = ap.resolve_workspace("owner/name", str(tmp_path), fr)
    assert ctx.fork_owner is None
    assert not any(c[:3] == ["gh", "api", "user"] for c in fr.calls)


def test_resolve_default_branch_gh_fallback(tmp_path):
    (tmp_path / ".git").mkdir()
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "symbolic-ref"],
         ap.RunResult(1, "", "no HEAD")),
        (["gh", "repo", "view"], ap.RunResult(0, "trunk\n", "")),
    ])
    ctx = ap.resolve_workspace("owner/name", str(tmp_path), fr)
    assert ctx.default_branch == "trunk"


def test_process_item_opens_on_approve(tmp_path):
    ctx = _ctx(tmp_path)
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "diff"], ap.RunResult(0, "patch", "")),
        (["gh", "pr", "create"],
         ap.RunResult(0, "https://github.com/o/n/pull/1", "")),
        (["claude"], ap.RunResult(0, '{"result": "APPROVE good"}', "")),
    ])
    fixer = ap.Engine("codex", "high", fr)
    verifier = ap.Engine("claude", "high", fr)
    item = ap.WorkItem("issue", "Fix bug", "b", "fix-bug", number=3, url="u")
    res = ap.process_item(ctx, item, fixer, verifier, fr, "guide")
    assert res.status == "opened"
    assert res.url == "https://github.com/o/n/pull/1"


def test_process_item_skips_after_max_revise(tmp_path):
    ctx = _ctx(tmp_path)
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "diff"], ap.RunResult(0, "patch", "")),
        (["claude"], ap.RunResult(0, '{"result": "REQUEST_CHANGES: nope"}', "")),
    ])
    fixer = ap.Engine("codex", "high", fr)
    verifier = ap.Engine("claude", "high", fr)
    item = ap.WorkItem("issue", "Fix bug", "b", "fix-bug")
    res = ap.process_item(ctx, item, fixer, verifier, fr, "guide", max_revise=1)
    assert res.status == "skipped"
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fr.calls)


def test_process_item_skips_on_empty_diff(tmp_path):
    ctx = _ctx(tmp_path)
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "diff"], ap.RunResult(0, "   \n", "")),
    ])
    fixer = ap.Engine("codex", "high", fr)
    verifier = ap.Engine("claude", "high", fr)
    item = ap.WorkItem("issue", "Fix bug", "b", "fix-bug")
    res = ap.process_item(ctx, item, fixer, verifier, fr, "g")
    assert res.status == "skipped"
    assert "no diff" in (res.reason or "")


def test_process_item_skips_when_pr_create_fails(tmp_path):
    ctx = _ctx(tmp_path)
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "diff"], ap.RunResult(0, "patch", "")),
        (["gh", "pr", "create"], ap.RunResult(1, "", "denied")),
        (["claude"], ap.RunResult(0, '{"result": "APPROVE"}', "")),
    ])
    fixer = ap.Engine("codex", "high", fr)
    verifier = ap.Engine("claude", "high", fr)
    item = ap.WorkItem("issue", "Fix bug", "b", "fix-bug")
    res = ap.process_item(ctx, item, fixer, verifier, fr, "g")
    assert res.status == "skipped"
    assert "pr creation failed" in (res.reason or "")


def test_process_item_dry_run_never_creates_pr(tmp_path):
    ctx = _ctx(tmp_path)
    fr = FakeRunner([
        (["git", "-C", str(tmp_path), "diff"], ap.RunResult(0, "patch", "")),
        (["claude"], ap.RunResult(0, '{"result": "APPROVE"}', "")),
    ])
    fixer = ap.Engine("codex", "high", fr)
    verifier = ap.Engine("claude", "high", fr)
    item = ap.WorkItem("issue", "Fix bug", "b", "fix-bug")
    res = ap.process_item(ctx, item, fixer, verifier, fr, "g", dry_run=True)
    assert res.status == "opened" and res.url is None
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fr.calls)


# --- Task 8: orchestrator --------------------------------------------------

def test_require_engines_raises_when_missing(monkeypatch):
    monkeypatch.setattr(ap.shutil, "which", lambda b: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        ap._require_engines()


def test_run_auto_pr_alternates_and_collects(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    captured: list[tuple[str, str]] = []

    def fake_process(ctx, item, fixer, verifier, runner, guidance, **kw):
        captured.append((fixer.name, verifier.name))
        return ap.PRResult(item, "opened", fixer.name, verifier.name,
                           url=f"https://github.com/o/n/pull/{len(captured)}")

    monkeypatch.setattr(ap, "resolve_workspace",
                        lambda *a, **k: ap.RepoCtx("o/n", "o/n", tmp_path, "main", "me"))
    monkeypatch.setattr(ap, "detect_or_bootstrap_guidance", lambda *a, **k: "g")
    monkeypatch.setattr(ap, "source_work_items",
                        lambda *a, **k: [ap.WorkItem("issue", f"t{i}", "", f"t{i}")
                                         for i in range(2)])
    monkeypatch.setattr(ap, "process_item", fake_process)

    out = ap.run_auto_pr("o/n", str(tmp_path), 2, "high", "high",
                         runner=FakeRunner())
    assert [r.url for r in out] == ["https://github.com/o/n/pull/1",
                                    "https://github.com/o/n/pull/2"]
    assert captured == [("claude", "codex"), ("codex", "claude")]


# --- Task 9: CLI -----------------------------------------------------------

def test_cli_auto_pr_prints_urls(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli
    item = ap.WorkItem("issue", "t", "", "t")
    monkeypatch.setattr("vouch.auto_pr.run_auto_pr", lambda *a, **k: [
        ap.PRResult(item, "opened", "claude", "codex",
                    url="https://github.com/o/n/pull/9"),
        ap.PRResult(item, "skipped", "codex", "claude", reason="gate red"),
    ])
    r = CliRunner().invoke(cli, [
        "auto-pr", "owner/name", "--workspace", str(tmp_path),
        "--count", "2", "--claude-effort", "high", "--codex-effort", "high",
    ])
    assert r.exit_code == 0, r.output
    assert "https://github.com/o/n/pull/9" in r.output
    assert "skipped" in r.output


def test_cli_auto_pr_json(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli
    item = ap.WorkItem("issue", "t", "", "t")
    monkeypatch.setattr("vouch.auto_pr.run_auto_pr", lambda *a, **k: [
        ap.PRResult(item, "opened", "claude", "codex",
                    url="https://github.com/o/n/pull/9"),
    ])
    r = CliRunner().invoke(cli, [
        "auto-pr", "owner/name", "--workspace", str(tmp_path),
        "--count", "1", "--claude-effort", "high", "--codex-effort", "high",
        "--json",
    ])
    assert r.exit_code == 0, r.output
    assert '"url"' in r.output and "pull/9" in r.output
