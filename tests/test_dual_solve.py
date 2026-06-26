"""tests for vouch dual-solve.

every test runs against a FakeRunner: no network, no real claude/codex/gh.
only the subprocess boundary is mocked; all stage logic is real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vouch import auto_pr as ap
from vouch import dual_solve as ds
from vouch.models import ContextItem, ContextPack, ProposalStatus
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


def test_parse_summary_splits_lines():
    text = "ROOT CAUSE: off-by-one in scan\nFIX: clamp the index"
    rc, fix = ds.parse_summary(text)
    assert rc == "off-by-one in scan" and fix == "clamp the index"


def test_parse_summary_tolerates_missing():
    rc, fix = ds.parse_summary("nothing structured here")
    assert rc == "" and fix == ""


def test_record_to_kb_proposes_cited_claims(tmp_path):
    store = KBStore.init(tmp_path)
    # codex engine returns a structured summary when asked read-only.
    fr = FakeRunner([(["codex"], ap.RunResult(
        0, "ROOT CAUSE: bad regex\nFIX: anchor the pattern", ""))])
    eng = ds.Engine("codex", "high", fr)
    issue = ds.Issue(title="Crash on empty input", body="b", number=12, url="u")
    chosen = ds.Candidate(engine="codex", branch="vouch-dual/12-x-codex",
                          worktree=tmp_path / "wt", diff="patch", sha="deadbeef",
                          ok=True)
    ids = ds.record_to_kb(store, issue, chosen, eng, "cleaner diff",
                          proposed_by="dual-solve")
    assert len(ids) == 3  # decision + root cause + fix pattern
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 3
    # every proposed claim cites the one registered source (validation passes).
    src_ids = {p.payload["evidence"][0] for p in pending}
    assert len(src_ids) == 1
    assert any("chose codex" in p.payload["text"] for p in pending)


def test_record_to_kb_decision_only_when_summary_blank(tmp_path):
    store = KBStore.init(tmp_path)
    fr = FakeRunner([(["codex"], ap.RunResult(0, "no structure", ""))])
    eng = ds.Engine("codex", "high", fr)
    issue = ds.Issue(title="t", body="", number=1)
    chosen = ds.Candidate(engine="codex", branch="b", worktree=tmp_path / "wt",
                          diff="d", sha="s", ok=True)
    ids = ds.record_to_kb(store, issue, chosen, eng, "", proposed_by="dual-solve")
    assert len(ids) == 1  # only the decision claim


def test_record_to_kb_coerces_nonascii_title(tmp_path):
    # a github issue title / engine summary with non-latin-1 chars (em dash,
    # smart quotes) must not crash the yaml write or leave a corrupt proposal:
    # storage encodes with the locale default, so claim text is coerced to ascii.
    store = KBStore.init(tmp_path)
    fr = FakeRunner([(["codex"],
                      ap.RunResult(0, "ROOT CAUSE: bad — regex\nFIX: anchor", ""))])
    eng = ds.Engine("codex", "high", fr)
    issue = ds.Issue(title="Fix — the “lexer”", body="b", number=8, url="u")
    chosen = ds.Candidate(engine="codex", branch="vouch-dual/8-x-codex",
                          worktree=tmp_path / "wt", diff="patch", sha="dead", ok=True)
    ids = ds.record_to_kb(store, issue, chosen, eng, "cleaner — diff",
                          proposed_by="dual-solve")
    assert len(ids) == 3
    # the writes succeeded and left no corrupt (zero-byte) proposal behind.
    pending = store.list_proposals(ProposalStatus.PENDING)
    assert len(pending) == 3
    for p in pending:
        # raises if any non-ascii survived into the yaml-written claim text.
        p.payload["text"].encode("ascii")
    assert any("--" in p.payload["text"] for p in pending)


def test_cleanup_removes_worktrees_and_loser_branch(tmp_path):
    fr = FakeRunner()
    c1 = ds.Candidate("claude", "vouch-dual/win", tmp_path / "a", ok=True)
    c2 = ds.Candidate("codex", "vouch-dual/lose", tmp_path / "b", ok=True)
    ds.cleanup(tmp_path, [c1, c2], {"vouch-dual/win"}, fr)
    removes = [c for c in fr.calls if c[3:5] == ["worktree", "remove"]]
    assert len(removes) == 2
    deletes = [c for c in fr.calls if c[3:5] == ["branch", "-D"]]
    assert deletes == [["git", "-C", str(tmp_path), "branch", "-D",
                        "vouch-dual/lose"]]


def test_prepare_runs_both_engines(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    monkeypatch.setattr(ds, "fetch_issue",
                        lambda ref, runner: ds.Issue("t", "b", number=1))
    monkeypatch.setattr(ds, "ground_prompt", lambda store, q, **k: "ctx")
    seen: list[str] = []

    def fake_rc(engine, issue, prompt, root, base, worktree, runner, *, commit=True):
        seen.append(engine.name)
        return ds.Candidate(engine.name, f"b-{engine.name}", worktree,
                            diff="d", ok=True)

    monkeypatch.setattr(ds, "run_candidate", fake_rc)
    _, cands, engines = ds.prepare(store, "o/n#1", tmp_path, FakeRunner(),
                                   workdir=tmp_path / "wd")
    assert seen == ["claude", "codex"]
    assert [c.engine for c in cands] == ["claude", "codex"]
    assert set(engines) == {"claude", "codex"}


def test_prepare_reports_progress(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    monkeypatch.setattr(ds, "fetch_issue",
                        lambda ref, runner: ds.Issue("Fix the parser", "b",
                                                     number=42))
    monkeypatch.setattr(ds, "ground_prompt", lambda store, q, **k: "ctx")

    def fake_rc(engine, issue, prompt, root, base, worktree, runner, *,
                commit=True):
        if engine.name == "codex":
            return ds.Candidate(engine.name, f"b-{engine.name}", worktree,
                                ok=False, error="engine produced no diff")
        return ds.Candidate(engine.name, f"b-{engine.name}", worktree,
                            diff="x\ny", ok=True)

    monkeypatch.setattr(ds, "run_candidate", fake_rc)
    msgs: list[str] = []
    ds.prepare(store, "o/n#42", tmp_path, FakeRunner(),
               workdir=tmp_path / "wd", on_progress=msgs.append)

    # the phases an operator needs to see while the engines work
    assert any("fetching issue" in m for m in msgs)
    assert any("#42" in m and "Fix the parser" in m for m in msgs)
    assert any("knowledge base" in m.lower() for m in msgs)
    # a "running" line then a completion line per engine, in order
    i_run_claude = next(i for i, m in enumerate(msgs)
                        if m.startswith("running claude"))
    i_done_claude = next(i for i, m in enumerate(msgs)
                         if m.startswith("claude:"))
    i_run_codex = next(i for i, m in enumerate(msgs)
                       if m.startswith("running codex"))
    assert i_run_claude < i_done_claude < i_run_codex
    # the ok line carries the diff size; the failed line carries the error
    assert any(m.startswith("claude:") and "2 lines" in m for m in msgs)
    assert any(m.startswith("codex:") and "engine produced no diff" in m
               for m in msgs)


def test_finalize_records_and_keeps_winner(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    monkeypatch.setattr(ds, "record_to_kb",
                        lambda *a, **k: ["prop-1", "prop-2"])
    fr = FakeRunner()
    win = ds.Candidate("codex", "vouch-dual/win", tmp_path / "a", ok=True)
    lose = ds.Candidate("claude", "vouch-dual/lose", tmp_path / "b", ok=True)
    engines = {"codex": ds.Engine("codex", "high", fr)}
    ids = ds.finalize(store, tmp_path, ds.Issue("t", "b", 1), win, engines,
                      [win, lose], "reason", fr, record=True, proposed_by="x")
    assert ids == ["prop-1", "prop-2"]
    assert any(c[3:5] == ["branch", "-D"] and "vouch-dual/lose" in c
               for c in fr.calls)
    assert not any(c[3:5] == ["branch", "-D"] and "vouch-dual/win" in c
                   for c in fr.calls)


def test_finalize_no_record_proposes_nothing(tmp_path, monkeypatch):
    store = KBStore.init(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(ds, "record_to_kb",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    win = ds.Candidate("codex", "b", tmp_path / "a", ok=True)
    ids = ds.finalize(store, tmp_path, ds.Issue("t", "b", 1), win, {}, [win],
                      "", FakeRunner(), record=False, proposed_by="x")
    assert ids == [] and called["n"] == 0


def test_cli_dual_solve_choose_claude(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli

    issue = ds.Issue("Fix bug", "b", number=4, url="u")
    c_claude = ds.Candidate("claude", "vouch-dual/4-fix-bug-claude",
                            tmp_path / "a", diff="DIFF-CLAUDE", sha="s1", ok=True)
    c_codex = ds.Candidate("codex", "vouch-dual/4-fix-bug-codex",
                           tmp_path / "b", diff="DIFF-CODEX", sha="s2", ok=True)
    monkeypatch.setattr("vouch.dual_solve._require_engines", lambda: None)
    monkeypatch.setattr("vouch.dual_solve.repo_root", lambda r, c: tmp_path)
    monkeypatch.setattr("vouch.dual_solve.prepare",
                        lambda *a, **k: (issue, [c_claude, c_codex], {}))
    captured = {}
    monkeypatch.setattr(
        "vouch.dual_solve.finalize",
        lambda store, root, iss, chosen, engines, cands, reason, runner, *, record, proposed_by:
            captured.update(chosen=chosen, record=record, reason=reason) or ["prop-1"])
    monkeypatch.setattr("vouch.cli._load_store", lambda *a, **k: object())

    # input: choose claude, then a one-line reason for the decision claim.
    r = CliRunner().invoke(cli, ["dual-solve", "o/n#4"], input="c\ncleaner\n")
    assert r.exit_code == 0, r.output
    assert "DIFF-CLAUDE" in r.output and "DIFF-CODEX" in r.output
    assert captured["chosen"].engine == "claude"
    assert captured["record"] is True
    assert "prop-1" in r.output


def test_cli_dual_solve_json_is_noninteractive(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli

    issue = ds.Issue("t", "b", number=1)
    cands = [ds.Candidate("claude", "b1", tmp_path / "a", diff="DA", ok=True),
             ds.Candidate("codex", "b2", tmp_path / "b", diff="DB", ok=True)]
    monkeypatch.setattr("vouch.dual_solve._require_engines", lambda: None)
    monkeypatch.setattr("vouch.dual_solve.repo_root", lambda r, c: tmp_path)
    monkeypatch.setattr("vouch.dual_solve.prepare",
                        lambda *a, **k: (issue, cands, {}))
    monkeypatch.setattr("vouch.cli._load_store", lambda *a, **k: object())
    finalize_called = {"n": 0}
    monkeypatch.setattr("vouch.dual_solve.finalize",
                        lambda *a, **k: finalize_called.__setitem__("n", 1))

    r = CliRunner().invoke(cli, ["dual-solve", "o/n#1", "--json"])
    assert r.exit_code == 0, r.output
    assert '"engine"' in r.output and "DA" in r.output and "DB" in r.output
    # --json must not prompt and must not finalize/record.
    assert finalize_called["n"] == 0


def test_cli_dual_solve_sandbox_uses_docker_runner(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli

    issue = ds.Issue("t", "b", number=1)
    captured: dict = {}

    class FakeSandboxRunner:
        def __init__(self, *, repo_root, runner, image):
            captured["repo_root"] = repo_root
            captured["base_runner"] = runner
            captured["image"] = image

    def fake_require(*, sandboxed=False, sandbox_image="", runner=None):
        captured["required"] = (sandboxed, sandbox_image, runner)

    def fake_prepare(store, issue_ref, root, runner, **kwargs):
        captured["runner"] = runner
        return issue, [], {}

    monkeypatch.setattr("vouch.dual_solve._require_engines", fake_require)
    monkeypatch.setattr("vouch.dual_solve.repo_root", lambda r, c: tmp_path)
    monkeypatch.setattr("vouch.dual_solve.prepare", fake_prepare)
    monkeypatch.setattr("vouch.sandbox.DockerAgentRunner", FakeSandboxRunner)
    monkeypatch.setattr("vouch.cli._load_store", lambda *a, **k: object())

    r = CliRunner().invoke(
        cli,
        ["dual-solve", "o/n#1", "--sandbox", "--sandbox-image", "agent-img", "--json"],
    )
    assert r.exit_code == 0, r.output
    assert captured["required"][0] is True
    assert captured["required"][1] == "agent-img"
    assert captured["repo_root"] == tmp_path
    assert captured["image"] == "agent-img"
    assert isinstance(captured["runner"], FakeSandboxRunner)


def test_cli_dual_solve_aborts_when_both_fail(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from vouch.cli import cli

    issue = ds.Issue("t", "b", number=1)
    cands = [ds.Candidate("claude", "b1", tmp_path / "a", ok=False, error="boom"),
             ds.Candidate("codex", "b2", tmp_path / "b", ok=False, error="boom")]
    monkeypatch.setattr("vouch.dual_solve._require_engines", lambda: None)
    monkeypatch.setattr("vouch.dual_solve.repo_root", lambda r, c: tmp_path)
    monkeypatch.setattr("vouch.dual_solve.prepare",
                        lambda *a, **k: (issue, cands, {}))
    monkeypatch.setattr("vouch.cli._load_store", lambda *a, **k: object())
    r = CliRunner().invoke(cli, ["dual-solve", "o/n#1"])
    assert r.exit_code != 0
    assert "both engines failed" in r.output.lower()
