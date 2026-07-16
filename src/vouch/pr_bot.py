"""Deterministic decision logic for the AI auto-merge bot.

Pure stdlib — no model dependency, no vouch-runtime imports. The CI workflows
call ``python -m vouch.pr_bot <subcommand>`` for every decision that must be
trustworthy: an author's trust tier, whether a PR touches core/ui paths, whether
a UI PR carries before/after screenshots, and whether a labeled PR may arm
native auto-merge. Claude Code verification runs as a GitHub Action, not here —
this module only makes the deterministic calls that gate it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence

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
    """Classify a changed-file list. Precedence: core > ui > code.

    Callers that build the changed-file list MUST include rename sources (GitHub REST `previous_filename` / git name-status old path). GraphQL `gh pr view --json files` omits them and can mis-classify core renames (#505).
    """
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


def _read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch.pr_bot")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify")
    c.add_argument("--files-file", required=True)
    c.add_argument("--print-klass", action="store_true")

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

    ns = p.parse_args(argv)

    if ns.cmd == "classify":
        changed = _read_lines(ns.files_file)
        sys.stdout.write(klass(changed) if ns.print_klass else json.dumps(classify(changed)))
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
