"""vouch dual-solve: run claude + codex on one issue; the operator picks a winner.

a sibling tool in the spirit of auto_pr -- it orchestrates the two coding
engines through the same injectable Runner so every stage is unit-testable
against a fake. unlike auto_pr it DOES touch the KB, but only ever via
``proposals.propose_claim`` (writes land in ``proposed/``), so the review gate
is preserved: nothing is auto-approved.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .auto_pr import Runner

__all__ = ["Candidate", "Issue", "fetch_issue", "parse_issue_ref"]


def _require_engines() -> None:
    """Fail fast before any worktree is created if a required binary is absent."""
    missing = [b for b in ("git", "gh", "claude", "codex") if shutil.which(b) is None]
    if missing:
        raise RuntimeError(
            f"required CLI not on PATH: {', '.join(missing)} "
            "(dual-solve needs git, gh, and both engines claude and codex)"
        )


@dataclass(frozen=True)
class Issue:
    title: str
    body: str
    number: int | None = None
    url: str | None = None


@dataclass
class Candidate:
    engine: str            # "claude" | "codex"
    branch: str
    worktree: Path
    diff: str = ""
    sha: str = ""
    ok: bool = False
    error: str | None = None


def parse_issue_ref(ref: str) -> tuple[str | None, str]:
    """Normalize an issue reference for ``gh issue view``.

    ``owner/name#42`` -> ``("owner/name", "42")`` (needs ``--repo``).
    a github issue URL -> ``(None, url)`` (gh accepts the URL directly).
    anything else is a hard error rather than a silent bad gh call.
    """
    r = ref.strip()
    m = re.fullmatch(r"([^/\s]+/[^/\s]+)#(\d+)", r)
    if m:
        return m.group(1), m.group(2)
    if re.search(r"github\.com/[^/\s]+/[^/\s]+/issues/\d+", r):
        return None, r
    raise ValueError(f"cannot parse github issue from {ref!r}")


def fetch_issue(ref: str, runner: Runner) -> Issue:
    """Fetch issue title and body via ``gh issue view``.

    Shells out to ``gh issue view`` through an injectable ``Runner`` so it is
    testable against ``FakeRunner`` (no network). The runner must return a
    ``RunResult`` with a json stdout on success (code 0).
    """
    repo, locator = parse_issue_ref(ref)
    argv = ["gh", "issue", "view", locator, "--json", "number,title,body,url"]
    if repo:
        argv += ["--repo", repo]
    res = runner.run(argv)
    if res.code != 0:
        raise RuntimeError(
            f"could not fetch issue {ref!r}: {res.stderr.strip()[:300]}"
        )
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh returned non-json for {ref!r}") from e
    return Issue(
        title=str(data.get("title", "")).strip(),
        body=str(data.get("body", "") or ""),
        number=data.get("number"),
        url=data.get("url"),
    )
