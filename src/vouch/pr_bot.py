"""Deterministic decision logic for the AI auto-merge bot.

Pure stdlib — no model dependency, no vouch-runtime imports. The CI workflows
call ``python -m vouch.pr_bot <subcommand>`` for every decision that must be
trustworthy: an author's trust tier, whether a PR touches core/ui paths, whether
a UI PR carries before/after screenshots, and whether a labeled PR may arm
native auto-merge. Claude Code verification runs as a GitHub Action, not here —
this module only makes the deterministic calls that gate it.
"""
from __future__ import annotations

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
    """Classify a changed-file list. Precedence: core > ui > code."""
    is_core = _touches(changed, CORE_GLOBS)
    is_ui = (not is_core) and _touches(changed, UI_GLOBS)
    return {"is_core": is_core, "is_ui": is_ui, "is_code": not is_core and not is_ui}


def klass(changed: Sequence[str]) -> str:
    c = classify(changed)
    return "core" if c["is_core"] else "ui" if c["is_ui"] else "code"


def is_trusted(author_association: str, actor: str) -> bool:
    return author_association == _OWNER_ASSOCIATION or actor in _BOT_ACTORS
