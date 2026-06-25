"""tests for vouch dual-solve.

every test runs against a FakeRunner: no network, no real claude/codex/gh.
only the subprocess boundary is mocked; all stage logic is real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vouch import auto_pr as ap
from vouch import dual_solve as ds
from vouch.models import ContextItem, ContextPack
from vouch.storage import KBStore


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


def test_parse_issue_ref_owner_repo_shorthand():
    assert ds.parse_issue_ref("owner/name#42") == ("owner/name", "42")


def test_parse_issue_ref_url_passes_through():
    url = "https://github.com/owner/name/issues/42"
    assert ds.parse_issue_ref(url) == (None, url)


def test_parse_issue_ref_rejects_garbage():
    with pytest.raises(ValueError):
        ds.parse_issue_ref("not an issue")


def test_require_engines_raises_when_missing(monkeypatch):
    monkeypatch.setattr(ds.shutil, "which", lambda b: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        ds._require_engines()


def test_fetch_issue_url_no_repo_flag():
    payload = '{"number": 7, "title": "Bug in parser", "body": "boom", "url": "u"}'
    fr = FakeRunner([(["gh", "issue", "view"], ap.RunResult(0, payload, ""))])
    issue = ds.fetch_issue("https://github.com/o/n/issues/7", fr)
    assert issue.number == 7 and issue.title == "Bug in parser"
    view = next(c for c in fr.calls if c[:3] == ["gh", "issue", "view"])
    assert "--repo" not in view


def test_fetch_issue_shorthand_adds_repo_flag():
    payload = '{"number": 9, "title": "t", "body": "", "url": "u"}'
    fr = FakeRunner([(["gh", "issue", "view"], ap.RunResult(0, payload, ""))])
    ds.fetch_issue("o/n#9", fr)
    view = next(c for c in fr.calls if c[:3] == ["gh", "issue", "view"])
    assert "--repo" in view and "o/n" in view and "9" in view


def test_fetch_issue_raises_on_gh_error():
    fr = FakeRunner([(["gh", "issue", "view"], ap.RunResult(1, "", "not found"))])
    with pytest.raises(RuntimeError, match="could not fetch issue"):
        ds.fetch_issue("https://github.com/o/n/issues/1", fr)


def test_repo_root_returns_toplevel():
    fr = FakeRunner([(["git", "-C", "/w", "rev-parse", "--show-toplevel"],
                      ap.RunResult(0, "/repo/root\n", ""))])
    assert ds.repo_root(fr, Path("/w")) == Path("/repo/root")


def test_repo_root_raises_outside_git():
    fr = FakeRunner([(["git", "-C", "/w", "rev-parse"],
                      ap.RunResult(128, "", "not a git repo"))])
    with pytest.raises(RuntimeError, match="not inside a git repository"):
        ds.repo_root(fr, Path("/w"))


def test_ground_prompt_renders_items(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    pack = ContextPack(query="q", items=[
        ContextItem(id="c1", type="claim", summary="auth uses jwt"),
    ])
    monkeypatch.setattr(ds, "build_context_pack", lambda *a, **k: pack)
    out = ds.ground_prompt(store, "auth")
    assert "[c1]" in out and "auth uses jwt" in out


def test_ground_prompt_empty_is_explicit(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    monkeypatch.setattr(ds, "build_context_pack",
                        lambda *a, **k: ContextPack(query="q", items=[]))
    assert "nothing" in ds.ground_prompt(store, "x").lower()


def test_build_prompt_includes_issue_and_grounding():
    issue = ds.Issue(title="Fix the lexer", body="it crashes", number=5)
    p = ds.build_prompt(issue, "- [c1] relevant claim")
    assert "Fix the lexer" in p and "it crashes" in p
    assert "[c1] relevant claim" in p
    assert "smallest correct change" in p


def _issue():
    return ds.Issue(title="Fix bug", body="b", number=3, url="u")


def test_run_candidate_success_commits_and_captures_sha(tmp_path):
    root, wt = tmp_path, tmp_path / "wt-claude"
    fr = FakeRunner([
        (["git", "-C", str(wt), "diff", "HEAD"], ap.RunResult(0, "patch text", "")),
        (["git", "-C", str(wt), "rev-parse", "HEAD"], ap.RunResult(0, "abc123\n", "")),
        (["claude"], ap.RunResult(0, '{"result": "done"}', "")),
    ])
    eng = ds.Engine("claude", "high", fr)
    cand = ds.run_candidate(eng, _issue(), "do it", root, "HEAD", wt, fr)
    assert cand.ok is True
    assert cand.engine == "claude"
    assert cand.branch == "vouch-dual/3-fix-bug-claude"
    assert cand.diff == "patch text" and cand.sha == "abc123"
    assert any(c[:5] == ["git", "-C", str(root), "worktree", "add"] for c in fr.calls)
    assert any(c[:4] == ["git", "-C", str(wt), "commit"] for c in fr.calls)
    assert any(c and c[0] == "claude" for c in fr.calls)


def test_run_candidate_worktree_add_failure(tmp_path):
    root, wt = tmp_path, tmp_path / "wt"
    fr = FakeRunner([(["git", "-C", str(root), "worktree", "add"],
                      ap.RunResult(1, "", "branch exists"))])
    cand = ds.run_candidate(ds.Engine("codex", "high", fr), _issue(),
                            "p", root, "HEAD", wt, fr)
    assert cand.ok is False and "worktree add failed" in (cand.error or "")


def test_run_candidate_empty_diff(tmp_path):
    root, wt = tmp_path, tmp_path / "wt"
    fr = FakeRunner([(["git", "-C", str(wt), "diff", "HEAD"],
                      ap.RunResult(0, "   \n", ""))])
    cand = ds.run_candidate(ds.Engine("codex", "high", fr), _issue(),
                            "p", root, "HEAD", wt, fr)
    assert cand.ok is False and "no diff" in (cand.error or "")
    assert not any(c[:4] == ["git", "-C", str(wt), "commit"] for c in fr.calls)
    assert any(c[:5] == ["git", "-C", str(root), "worktree", "add"] for c in fr.calls)


def test_run_candidate_dry_run_skips_commit(tmp_path):
    root, wt = tmp_path, tmp_path / "wt"
    fr = FakeRunner([(["git", "-C", str(wt), "diff", "HEAD"],
                      ap.RunResult(0, "patch", ""))])
    cand = ds.run_candidate(ds.Engine("codex", "high", fr), _issue(),
                            "p", root, "HEAD", wt, fr, commit=False)
    assert cand.ok is True and cand.diff == "patch" and cand.sha == ""
    assert not any(c[:4] == ["git", "-C", str(wt), "commit"] for c in fr.calls)


def test_run_candidate_engine_crash_is_caught(tmp_path):
    root, wt = tmp_path, tmp_path / "wt"

    class Boom:
        def run(self, argv, *, cwd=None, stdin=None, timeout=None):
            if argv[0] in ("claude", "codex"):
                raise RuntimeError("engine binary exploded")
            return ap.RunResult(0, "", "")

    boom = Boom()
    cand = ds.run_candidate(ds.Engine("codex", "high", boom), _issue(),
                            "p", root, "HEAD", wt, boom)
    assert cand.ok is False and "engine failed" in (cand.error or "")
