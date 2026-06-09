"""PR cache: dedup raises against merged/closed PRs of a target repo.

Covers parsing, cache round-trip, dedup scoring, the gh-driven build flow
(with subprocess fully mocked — no network), and the CLI surface.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from vouch import pr_cache as prc
from vouch.cli import cli

# --- pure helpers ---------------------------------------------------------


@pytest.mark.parametrize("ref, owner, name", [
    ("https://github.com/vouchdev/vouch", "vouchdev", "vouch"),
    ("https://github.com/vouchdev/vouch.git", "vouchdev", "vouch"),
    ("https://github.com/vouchdev/vouch/pull/104", "vouchdev", "vouch"),
    ("http://github.com/foo-bar/baz_qux", "foo-bar", "baz_qux"),
    ("git@github.com:vouchdev/vouch.git", "vouchdev", "vouch"),
    ("vouchdev/vouch", "vouchdev", "vouch"),
])
def test_parse_repo_accepts_all_url_shapes(ref: str, owner: str, name: str) -> None:
    r = prc.parse_repo(ref)
    assert (r.owner, r.name) == (owner, name)
    assert r.slug == f"{owner}/{name}"
    assert r.cache_key == f"{owner}__{name}"


@pytest.mark.parametrize("ref", [
    "", "   ", "not a url", "https://gitlab.com/x/y",
    "https://github.com//repo", "https://github.com/owner/",
])
def test_parse_repo_rejects_bad_input(ref: str) -> None:
    with pytest.raises(ValueError):
        prc.parse_repo(ref)


def test_extract_issue_refs_dedups_and_filters_short() -> None:
    body = "fixes #15028 and #15028 (duplicate), also see #15145 #9 (too short)"
    refs = prc._extract_issue_refs(body)
    assert refs == [15028, 15145]  # #9 filtered (< 2 digits), dup collapsed


def test_extract_issue_refs_empty_body_is_empty() -> None:
    assert prc._extract_issue_refs("") == []
    assert prc._extract_issue_refs(None) == []  # type: ignore[arg-type]


def test_truncate_under_and_over() -> None:
    assert prc._truncate("short", 100) == "short"
    long = "x" * 50
    out = prc._truncate(long, 10)
    assert len(out) == 10
    assert out.endswith("…")


# --- cache round-trip -----------------------------------------------------


def _sample_record(num: int, state: str = "merged", **kw: Any) -> prc.PRRecord:
    return prc.PRRecord(
        number=num,
        state=state,
        title=kw.get("title", f"feat: thing {num}"),
        body_excerpt=kw.get("body", "fixes #1"),
        author=kw.get("author", "octocat"),
        files=kw.get("files", ["src/a.py"]),
        labels=kw.get("labels", ["bug"]),
        issue_refs=kw.get("issue_refs", [1]),
        merged_at="2026-01-01T00:00:00Z" if state == "merged" else None,
        closed_at="2026-01-01T00:00:00Z" if state == "closed" else None,
        url=f"https://github.com/o/r/pull/{num}",
        close_analysis=kw.get("close_analysis"),
    )


def test_cache_roundtrip_preserves_close_analysis(tmp_path: Path) -> None:
    repo = prc.RepoRef("octo", "repo")
    path = prc.cache_path_for(repo, tmp_path)
    records = {
        "100": _sample_record(100, state="merged"),
        "101": _sample_record(
            101, state="closed",
            close_analysis=prc.CloseAnalysis(
                reason="superseded by #102",
                do_not_repeat=["don't drop the accessible() check"],
                confidence="high",
                analyzer="claude-cli",
                analyzed_at="2026-06-04T00:00:00Z",
            ),
        ),
    }
    prc.save_cache(path, repo, records)
    loaded = prc.load_cache(path)
    assert set(loaded) == {"100", "101"}
    assert loaded["101"].close_analysis is not None
    assert loaded["101"].close_analysis.do_not_repeat == ["don't drop the accessible() check"]
    assert loaded["101"].close_analysis.analyzer == "claude-cli"


def test_load_cache_missing_file_is_empty(tmp_path: Path) -> None:
    assert prc.load_cache(tmp_path / "nope.json") == {}


def test_save_cache_atomic_via_tmp_swap(tmp_path: Path) -> None:
    """Writes go through a .tmp sibling so a partial write never replaces a
    good cache file. We assert by writing twice and confirming no .tmp leak."""
    repo = prc.RepoRef("o", "r")
    path = prc.cache_path_for(repo, tmp_path)
    prc.save_cache(path, repo, {"1": _sample_record(1)})
    prc.save_cache(path, repo, {"1": _sample_record(1), "2": _sample_record(2)})
    leftovers = list(path.parent.glob("*.tmp"))
    assert leftovers == [], leftovers
    assert len(prc.load_cache(path)) == 2


def test_cache_path_respects_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    p = prc.cache_path_for(prc.RepoRef("o", "r"))
    assert p == tmp_path / "o__r.json"


# --- dedup scoring --------------------------------------------------------


def test_check_duplicates_title_only_match() -> None:
    cache = {
        "1": _sample_record(1, title="restore accessible() check on document preview"),
        "2": _sample_record(2, title="add new feature flag for embeddings"),
    }
    cands = prc.check_duplicates(cache, topic="document preview accessible restored", files=[])
    assert cands, "expected at least one match by title overlap"
    assert cands[0].pr.number == 1


def test_check_duplicates_path_overlap_breaks_ties() -> None:
    doc_api = "api/apps/restful_apis/document_api.py"
    cache = {
        "1": _sample_record(1, title="generic fix", files=[doc_api]),
        "2": _sample_record(2, title="generic fix", files=["docs/intro.md"]),
    }
    cands = prc.check_duplicates(
        cache, topic="generic fix", files=[doc_api], min_score=0.0,
    )
    # Both share title; only #1 shares the file path. #1 must rank first.
    assert cands[0].pr.number == 1
    assert cands[0].path_overlap > cands[1].path_overlap


def test_check_duplicates_closed_pr_outranks_merged_on_tie() -> None:
    """Closed-not-merged PRs carry the 'don't repeat' signal — when two
    candidates score equally, the rejected attempt must surface first
    (it's the higher-value signal for a contributor about to raise a PR)."""
    cache = {
        "1": _sample_record(1, state="merged", title="adopt richer scopes"),
        "2": _sample_record(2, state="closed", title="adopt richer scopes"),
    }
    cands = prc.check_duplicates(cache, topic="adopt richer scopes", files=[])
    assert [c.pr.number for c in cands] == [2, 1]
    assert cands[0].score == cands[1].score  # tie; tiebreak ordered closed first


def test_check_duplicates_respects_min_score() -> None:
    cache = {"1": _sample_record(1, title="totally unrelated stuff")}
    cands = prc.check_duplicates(cache, topic="something else entirely", min_score=0.9)
    assert cands == []


# --- analyzer dispatch (no real network / CLI) ----------------------------


def test_analyzer_skips_when_neither_claude_nor_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(prc.shutil, "which", lambda _name: None)  # no claude on PATH
    result = prc.analyze_close_reason(_sample_record(1, state="closed"), "some comments")
    assert result is None


def _fake_claude_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prc.shutil, "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )


def _fake_run_returning(stdout: str) -> Any:
    def _fake_run(cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=0, stdout=stdout, stderr="",
        )
    return _fake_run


def test_analyzer_parses_well_formed_claude_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pretend claude CLI is on PATH and returns the contract JSON.
    _fake_claude_on_path(monkeypatch)
    payload = json.dumps({
        "reason": "approach rejected: would break tenant isolation",
        "do_not_repeat": [
            "remove accessible() check",
            "merge into single endpoint",
        ],
        "confidence": "high",
    })
    monkeypatch.setattr(prc.subprocess, "run", _fake_run_returning(payload))
    ca = prc.analyze_close_reason(_sample_record(1, state="closed"), "comments here")
    assert ca is not None
    assert ca.analyzer == "claude-cli"
    assert ca.confidence == "high"
    assert "tenant isolation" in ca.reason
    assert "remove accessible() check" in ca.do_not_repeat


def test_analyzer_tolerates_prose_around_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_claude_on_path(monkeypatch)
    inner = '{"reason":"x","do_not_repeat":[],"confidence":"low"}'
    stdout = f"Sure! Here is the analysis:\n{inner}\nThanks!"
    monkeypatch.setattr(prc.subprocess, "run", _fake_run_returning(stdout))
    ca = prc.analyze_close_reason(_sample_record(1, state="closed"), "")
    assert ca is not None and ca.reason == "x"


def test_analyzer_returns_low_confidence_on_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_claude_on_path(monkeypatch)
    monkeypatch.setattr(prc.subprocess, "run", _fake_run_returning("totally not json"))
    ca = prc.analyze_close_reason(_sample_record(1, state="closed"), "")
    assert ca is not None
    assert ca.confidence == "low"
    assert "unparseable" in ca.reason.lower()


# --- build (gh fully mocked) ----------------------------------------------


def _fake_gh_factory(merged: list[dict[str, Any]], closed: list[dict[str, Any]],
                     files_by_num: dict[int, list[str]]) -> Any:
    """Routes ``gh pr list`` / ``gh pr view`` calls without touching the network."""
    def _fake_run(cmd: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
        args = list(cmd)
        assert args[0] == "gh", args

        def _ok(stdout: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=stdout, stderr="",
            )

        if args[1:3] == ["pr", "list"]:
            state_idx = args.index("--state") + 1
            st = args[state_idx]
            rows = merged if st == "merged" else closed if st == "closed" else []
            return _ok(json.dumps(rows))
        if args[1:3] == ["pr", "view"]:
            num = int(args[3])
            wants = args[args.index("--json") + 1]
            payload: dict[str, Any] = {}
            if "files" in wants:
                payload["files"] = [{"path": p} for p in files_by_num.get(num, [])]
            if "comments" in wants or "reviews" in wants:
                payload["comments"] = []
                payload["reviews"] = []
            return _ok(json.dumps(payload))
        return subprocess.CompletedProcess(
            args=args, returncode=2, stdout="", stderr="unhandled gh call",
        )
    return _fake_run


def test_build_merges_into_cache_and_skips_analysis_when_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(prc.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    fake = _fake_gh_factory(
        merged=[{
            "number": 100, "state": "MERGED", "title": "feat: thing",
            "body": "fixes #42", "author": {"login": "alice"},
            "labels": [{"name": "bug"}], "mergedAt": "2026-01-01T00:00:00Z",
            "closedAt": None, "url": "https://github.com/o/r/pull/100",
        }],
        closed=[{
            "number": 101, "state": "CLOSED", "title": "chore: nope",
            "body": "won't work", "author": {"login": "bob"},
            "labels": [], "mergedAt": None,
            "closedAt": "2026-02-01T00:00:00Z",
            "url": "https://github.com/o/r/pull/101",
        }],
        files_by_num={100: ["src/a.py"], 101: ["src/b.py"]},
    )
    monkeypatch.setattr(prc.subprocess, "run", fake)
    result = prc.build(prc.RepoRef("o", "r"), state="all", limit=10,
                       analyze_closed=False, cache_dir=tmp_path)
    assert result.fetched == 2
    assert result.new == 2
    assert result.analyzed == 0
    cache = prc.load_cache(result.path)
    assert cache["100"].state == "merged"
    assert cache["101"].state == "closed"
    assert cache["100"].files == ["src/a.py"]
    assert cache["100"].issue_refs == [42]


def test_build_is_idempotent_and_preserves_prior_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(prc.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    closed_row = {
        "number": 101, "state": "CLOSED", "title": "x",
        "body": "", "author": {"login": "bob"}, "labels": [],
        "mergedAt": None, "closedAt": "2026-02-01T00:00:00Z",
        "url": "https://github.com/o/r/pull/101",
    }
    fake = _fake_gh_factory(merged=[], closed=[closed_row], files_by_num={101: []})
    monkeypatch.setattr(prc.subprocess, "run", fake)

    # First build: no analysis.
    prc.build(prc.RepoRef("o", "r"), state="closed", limit=10,
              analyze_closed=False, cache_dir=tmp_path)

    # Hand-graft a prior analysis to confirm reruns preserve it.
    path = prc.cache_path_for(prc.RepoRef("o", "r"), tmp_path)
    cache = prc.load_cache(path)
    cache["101"].close_analysis = prc.CloseAnalysis(
        reason="manually annotated", do_not_repeat=["X"], confidence="medium",
        analyzer="claude-cli", analyzed_at="2026-06-04T00:00:00Z",
    )
    prc.save_cache(path, prc.RepoRef("o", "r"), cache)

    # Second build with analyze_closed=True but reanalyze=False — must NOT
    # clobber. (no claude / no key, so analyzer would no-op anyway, but the
    # contract is that the existing record stays.)
    monkeypatch.setattr(
        prc.shutil, "which",
        lambda name: "/usr/bin/gh" if name == "gh" else None,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prc.build(prc.RepoRef("o", "r"), state="closed", limit=10,
              analyze_closed=True, reanalyze=False, cache_dir=tmp_path)
    again = prc.load_cache(path)
    assert again["101"].close_analysis is not None
    assert again["101"].close_analysis.reason == "manually annotated"


def test_build_raises_clean_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(prc.shutil, "which", lambda _name: None)
    with pytest.raises(prc.GHError, match="gh"):
        prc.build(prc.RepoRef("o", "r"), state="merged", cache_dir=tmp_path)


# --- CLI surface ----------------------------------------------------------


def test_cli_check_no_cache_yet_returns_no_match(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, [
        "pr-cache", "check", "o/r", "--topic", "anything",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "no_match"
    assert payload["cache_size"] == 0


def test_cli_check_likely_duplicate_against_closed_pr(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    repo = prc.RepoRef("o", "r")
    rec = _sample_record(
        42, state="closed",
        title="restore accessible check on document preview endpoint",
        files=["api/apps/restful_apis/document_api.py"],
        close_analysis=prc.CloseAnalysis(
            reason="reviewer asked to fold into #15146 instead",
            do_not_repeat=["don't open a second PR for the same route"],
            confidence="high", analyzer="claude-cli", analyzed_at="2026-06-04T00:00:00Z",
        ),
    )
    prc.save_cache(prc.cache_path_for(repo, tmp_path), repo, {"42": rec})
    result = CliRunner().invoke(cli, [
        "pr-cache", "check", "o/r",
        "--topic", "restore accessible check document preview",
        "--files", "api/apps/restful_apis/document_api.py",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "likely_duplicate"
    top = payload["candidates"][0]
    assert top["number"] == 42
    assert top["close_analysis"]["do_not_repeat"]


def test_cli_show_table_includes_close_reason(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    repo = prc.RepoRef("o", "r")
    rec = _sample_record(
        7, state="closed", title="bad idea",
        close_analysis=prc.CloseAnalysis(
            reason="approach broke tests", do_not_repeat=["skipping accessible()"],
            confidence="medium", analyzer="claude-cli", analyzed_at="2026-06-04T00:00:00Z",
        ),
    )
    prc.save_cache(prc.cache_path_for(repo, tmp_path), repo, {"7": rec})
    result = CliRunner().invoke(cli, ["pr-cache", "show", "o/r"])
    assert result.exit_code == 0, result.output
    assert "#7" in result.output
    assert "approach broke tests" in result.output
    assert "skipping accessible()" in result.output


def test_cli_show_json_emits_machine_readable(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    repo = prc.RepoRef("o", "r")
    prc.save_cache(prc.cache_path_for(repo, tmp_path), repo, {"7": _sample_record(7)})
    result = CliRunner().invoke(cli, ["pr-cache", "show", "o/r", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["prs"][0]["number"] == 7


def test_cli_bad_repo_url_is_clean_error() -> None:
    result = CliRunner().invoke(cli, ["pr-cache", "check", "not-a-url", "--topic", "x"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Error:" in result.output


def test_cli_build_surfaces_gh_missing_as_clean_error(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    monkeypatch.setenv("VOUCH_PR_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prc.shutil, "which", lambda _name: None)
    result = CliRunner().invoke(cli, ["pr-cache", "build", "o/r", "--state", "merged"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Error:" in result.output
    assert "gh" in result.output.lower()
