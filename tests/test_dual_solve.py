"""tests for vouch dual-solve.

every test runs against a FakeRunner: no network, no real claude/codex/gh.
only the subprocess boundary is mocked; all stage logic is real.
"""
from __future__ import annotations

import pytest

from vouch import auto_pr as ap
from vouch import dual_solve as ds


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
