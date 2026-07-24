"""Deterministic decision logic for the AI auto-merge bot.

Pure stdlib — no model dependency, no vouch-runtime imports. The CI workflows
call ``python -m vouch.pr_bot <subcommand>`` for every decision that must be
trustworthy: an author's trust tier, whether a PR touches core/ui paths, whether
a UI PR carries before/after screenshots, and whether a labeled PR may arm
native auto-merge. CodeRabbit is the review gate and runs as a GitHub App, not
here — this module only turns its verdict into the required `coderabbit-approved`
commit status and the deterministic calls that gate the merge.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

# the review-gate core: writes here are the north star. mirrored verbatim in
# .github/CODEOWNERS (test_pr_bot asserts parity). a PR touching any of these
# needs the owner's review and is never merged by automation.
CORE_GLOBS: tuple[str, ...] = (
    "src/vouch/proposals.py",
    "src/vouch/lifecycle.py",
    "src/vouch/storage.py",
    "src/vouch/audit.py",
    "src/vouch/models.py",
    "src/vouch/capabilities.py",
    "src/vouch/server.py",
    "src/vouch/jsonl_server.py",
    "src/vouch/http_server.py",
    "src/vouch/cli.py",
    "src/vouch/pr_bot.py",
    "src/vouch/migrations/**",
    "migrations/**",
    ".github/**",
)

# ui surfaces: reviewed by before/after screenshot, never by running the app.
UI_GLOBS: tuple[str, ...] = (
    "web/**",
    "src/vouch/web/**",
    "webapp/**",
)

_OWNER_ASSOCIATION = "OWNER"
_BOT_ACTORS = frozenset({"dependabot[bot]"})

# CodeRabbit is the required review gate (.coderabbit.yaml). only reviews it
# authors on github count; anyone else's approval never satisfies the gate.
CODERABBIT_LOGIN = "coderabbitai[bot]"

# a contributor gets STRIKE_LIMIT rounds of "changes requested" from CodeRabbit
# before the pr is auto-closed. the owner and bots are exempt (author_is_exempt).
STRIKE_LIMIT = 3

# a pr whose author leaves CodeRabbit's change request unaddressed (no new
# commit) for STALE_DAYS is auto-closed by the scheduled stale-pr-reaper.
STALE_DAYS = 2
_EXEMPT_AUTHORS = frozenset({"plind-junior"}) | _BOT_ACTORS


def _match(path: str, glob: str) -> bool:
    g = glob.lstrip("/")
    if g.endswith("/**"):
        prefix = g[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return path == g


def _touches(changed: Iterable[str], globs: Iterable[str]) -> bool:
    globs = tuple(globs)
    return any(_match(p, g) for p in changed for g in globs)


def classify(changed: Sequence[str]) -> dict[str, bool]:
    """Classify a changed-file list. Precedence: core > ui > code."""
    is_core = _touches(changed, CORE_GLOBS)
    is_ui = (not is_core) and _touches(changed, UI_GLOBS)
    return {"is_core": is_core, "is_ui": is_ui, "is_code": not is_core and not is_ui}


def klass(changed: Sequence[str]) -> str:
    c = classify(changed)
    return "core" if c["is_core"] else "ui" if c["is_ui"] else "code"


def is_trusted(author_association: str, actor: str) -> bool:
    return author_association == _OWNER_ASSOCIATION or actor in _BOT_ACTORS


_GH_IMAGE = re.compile(
    r"""(?:!\[[^\]]*\]\(\s*|<img\b[^>]*\bsrc\s*=\s*["']?)"""
    r"""(?:https?://(?:user-images\.githubusercontent\.com/"""
    r"""|github\.com/user-attachments/assets/"""
    r"""|github\.com/[^/\s"'>]+/[^/\s"'>]+/assets/))""",
    re.I,
)


def has_before_after_screenshots(body: str | None) -> bool:
    """True when the PR body embeds >=2 GitHub-hosted images (before + after)."""
    if not body:
        return False
    return len(_GH_IMAGE.findall(body)) >= 2


def should_arm_automerge(*, is_core: bool, ci_passing: bool,
                         claude_verdict: str, is_draft: bool) -> bool:
    """Deterministic arm gate. Claude can only veto — it never widens this."""
    if is_draft or is_core or not ci_passing:
        return False
    return claude_verdict == "APPROVE"


def _cr_verdicts(reviews: Sequence[Mapping[str, Any]], *,
                 login: str) -> list[tuple[str, Any]]:
    """(state, commit_id) for CodeRabbit reviews carrying a verdict.

    COMMENTED and DISMISSED reviews carry no verdict and are dropped.
    """
    out: list[tuple[str, Any]] = []
    for r in reviews:
        if (r.get("user") or {}).get("login") != login:
            continue
        state = str(r.get("state") or "").upper()
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            out.append((state, r.get("commit_id")))
    return out


def coderabbit_verdict(reviews: Sequence[Mapping[str, Any]], *,
                       head_sha: str | None = None,
                       login: str = CODERABBIT_LOGIN) -> tuple[str, int]:
    """CodeRabbit's (verdict, strikes) for a pr's review list.

    ``verdict`` is its stance on ``head_sha`` — 'approved', 'changes', or
    'pending' when it has not yet reviewed that commit (so a fresh push voids
    a prior approval). ``strikes`` counts the distinct commits it has requested
    changes on, i.e. failed review rounds, across the pr's whole history.
    """
    verdicts = _cr_verdicts(reviews, login=login)
    strikes = len({cid for state, cid in verdicts if state == "CHANGES_REQUESTED"})
    scoped = [v for v in verdicts if head_sha is None or v[1] == head_sha]
    if not scoped:
        return "pending", strikes
    return ("approved" if scoped[-1][0] == "APPROVED" else "changes"), strikes


def gate_status(verdict: str) -> str:
    """Commit-status state for the required `coderabbit-approved` check."""
    return {"approved": "success", "changes": "failure"}.get(verdict, "pending")


def author_is_exempt(author: str) -> bool:
    """The owner and bots are never auto-closed for failed reviews."""
    return author in _EXEMPT_AUTHORS


def should_close(verdict: str, strikes: int, *, author: str,
                 limit: int = STRIKE_LIMIT) -> bool:
    """Auto-close a contributor pr CodeRabbit has rejected `limit` rounds."""
    return (not author_is_exempt(author)
            and verdict == "changes"
            and strikes >= limit)


def _iso_epoch(s: str) -> float:
    """Epoch seconds for a github ISO8601 timestamp (e.g. 2026-07-15T10:20:30Z)."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def should_close_stale(reviews: Sequence[Mapping[str, Any]], *, head_sha: str,
                       now_epoch: float, author: str, days: int = STALE_DAYS,
                       login: str = CODERABBIT_LOGIN) -> bool:
    """Auto-close a pr whose author left a CodeRabbit change request unaddressed.

    Fires only when CodeRabbit's latest verdict *on the current head* is
    "changes requested" and that review is >= ``days`` old — i.e. no new commit
    has landed since (a push would move ``head_sha`` off the review's
    ``commit_id``). the owner and bots are exempt.
    """
    if author_is_exempt(author):
        return False
    on_head = [r for r in reviews
               if (r.get("user") or {}).get("login") == login
               and r.get("commit_id") == head_sha
               and str(r.get("state") or "").upper() in ("APPROVED", "CHANGES_REQUESTED")]
    if not on_head or str(on_head[-1].get("state") or "").upper() != "CHANGES_REQUESTED":
        return False
    submitted = on_head[-1].get("submitted_at")
    if not submitted:
        return False
    age_days = (now_epoch - _iso_epoch(str(submitted))) / 86400.0
    return age_days >= days


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def extract_changed_paths(files_json: str) -> list[str]:
    """flatten a REST ``/pulls/{n}/files`` payload into a path list.

    emits both ``filename`` and, for a renamed entry, ``previous_filename`` —
    a rename that lands a core path under a new name must still classify as
    core. ``gh pr view --json files`` (the GraphQL-backed shortcut) carries no
    previous-filename field and silently drops this; callers must use the
    REST files endpoint (``gh api repos/{o}/{r}/pulls/{n}/files``) instead.
    """
    paths: list[str] = []
    for entry in json.loads(files_json):
        filename = entry.get("filename")
        if filename:
            paths.append(filename)
        previous = entry.get("previous_filename")
        if previous:
            paths.append(previous)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch.pr_bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify")
    c.add_argument("--files-file", required=True)
    c.add_argument("--print-klass", action="store_true")

    cf = sub.add_parser("changed-files")
    cf.add_argument("--json-file", required=True)

    for name in ("core-touched", "ui-touched"):
        sp = sub.add_parser(name)
        sp.add_argument("--files-file", required=True)

    t = sub.add_parser("trust")
    t.add_argument("--author-association", required=True)
    t.add_argument("--actor", required=True)

    s = sub.add_parser("has-screenshots")
    s.add_argument("--body-file", required=True)

    a = sub.add_parser("should-arm")
    a.add_argument("--files-file", required=True)
    a.add_argument("--ci", required=True, choices=["passing", "failing"])
    a.add_argument("--verdict", required=True)
    a.add_argument("--draft", action="store_true")

    g = sub.add_parser("coderabbit-gate")
    g.add_argument("--reviews-file", required=True)
    g.add_argument("--head-sha", required=True)
    g.add_argument("--author", required=True)

    st = sub.add_parser("stale-check")
    st.add_argument("--reviews-file", required=True)
    st.add_argument("--head-sha", required=True)
    st.add_argument("--author", required=True)
    st.add_argument("--now-epoch", required=True, type=int)

    ns = p.parse_args(argv)

    if ns.cmd == "classify":
        changed = _read_lines(ns.files_file)
        sys.stdout.write(klass(changed) if ns.print_klass else json.dumps(classify(changed)))
        return 0
    if ns.cmd == "changed-files":
        with open(ns.json_file, encoding="utf-8") as fh:
            paths = extract_changed_paths(fh.read())
        sys.stdout.write("\n".join(paths))
        return 0
    if ns.cmd == "core-touched":
        return 0 if classify(_read_lines(ns.files_file))["is_core"] else 1
    if ns.cmd == "ui-touched":
        return 0 if _touches(_read_lines(ns.files_file), UI_GLOBS) else 1
    if ns.cmd == "trust":
        return 0 if is_trusted(ns.author_association, ns.actor) else 1
    if ns.cmd == "has-screenshots":
        with open(ns.body_file, encoding="utf-8") as fh:
            return 0 if has_before_after_screenshots(fh.read()) else 1
    if ns.cmd == "should-arm":
        c2 = classify(_read_lines(ns.files_file))
        ok = should_arm_automerge(is_core=c2["is_core"], ci_passing=ns.ci == "passing",
                                  claude_verdict=ns.verdict, is_draft=ns.draft)
        return 0 if ok else 1
    if ns.cmd == "coderabbit-gate":
        with open(ns.reviews_file, encoding="utf-8") as fh:
            loaded = json.load(fh)
        reviews = loaded if isinstance(loaded, list) else []
        verdict, strikes = coderabbit_verdict(reviews, head_sha=ns.head_sha)
        close = should_close(verdict, strikes, author=ns.author)
        sys.stdout.write(
            f"state={gate_status(verdict)}\n"
            f"verdict={verdict}\n"
            f"strikes={strikes}\n"
            f"close={'true' if close else 'false'}\n")
        return 0
    if ns.cmd == "stale-check":
        with open(ns.reviews_file, encoding="utf-8") as fh:
            loaded = json.load(fh)
        reviews = loaded if isinstance(loaded, list) else []
        stale = should_close_stale(reviews, head_sha=ns.head_sha,
                                   now_epoch=ns.now_epoch, author=ns.author)
        return 0 if stale else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
