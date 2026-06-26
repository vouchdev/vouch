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
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .auto_pr import Engine, Runner, slugify
from .context import build_context_pack
from .models import ContextPack, SourceType
from .proposals import propose_claim
from .sandbox import DEFAULT_SANDBOX_IMAGE, require_docker_sandbox
from .storage import KBStore

__all__ = [
    "Candidate",
    "Issue",
    "build_prompt",
    "cleanup",
    "fetch_issue",
    "finalize",
    "ground_prompt",
    "parse_issue_ref",
    "parse_summary",
    "prepare",
    "record_to_kb",
    "repo_root",
    "run_candidate",
]


def _require_engines(*, sandboxed: bool = False,
                     sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
                     runner: Runner | None = None) -> None:
    """Fail fast before any worktree is created if a required binary is absent."""
    bins = ("git", "gh", "docker") if sandboxed else ("git", "gh", "claude", "codex")
    missing = [b for b in bins if shutil.which(b) is None]
    if missing:
        raise RuntimeError(
            f"required CLI not on PATH: {', '.join(missing)} "
            "(dual-solve needs git, gh, and either host engines or docker sandbox mode)"
        )
    if sandboxed:
        require_docker_sandbox(sandbox_image, runner=runner)


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


_FIX_PROMPT = (
    "you are resolving a github issue in the current repository.\n\n"
    "issue #{num}: {title}\n\n{body}\n\n"
    "what the project's knowledge base already knows about this area:\n"
    "{grounding}\n\n"
    "make the smallest correct change that resolves this issue, including a "
    "regression test if the repo has tests. keep it to one logical change. "
    "do not add any AI-attribution trailer to commits."
)


def repo_root(runner: Runner, cwd: Path) -> Path:
    """Return the git repository root for a given working directory.

    Uses ``git rev-parse --show-toplevel`` through an injectable ``Runner``
    so it is testable. Raises ``RuntimeError`` if not in a git repository.
    """
    res = runner.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if res.code != 0:
        raise RuntimeError("not inside a git repository")
    return Path(res.stdout.strip())


def ground_prompt(store: KBStore, query: str, *, limit: int = 8) -> str:
    """Render knowledge base context into a bulleted prompt fragment.

    Calls ``build_context_pack`` to query the KB for items matching ``query``.
    Returns a string like:
      - [c1] auth uses jwt
      - [c2] session expiry is 1 hour
    If the KB has nothing for this query, returns an explicit message saying so.
    """
    pack = build_context_pack(store, query=query, limit=limit)
    items = pack.items if isinstance(pack, ContextPack) else []
    if not items:
        return "(the knowledge base has nothing on this topic yet.)"
    return "\n".join(f"- [{it.id}] {it.summary}" for it in items)


def build_prompt(issue: Issue, grounding: str) -> str:
    """Fill the shared fix prompt template with an issue and grounding text.

    Takes an ``Issue`` (title, body, number) and rendered grounding (from
    ``ground_prompt``), and returns the full prompt to send to a code engine.
    """
    return _FIX_PROMPT.format(
        num=issue.number if issue.number is not None else "?",
        title=issue.title,
        body=issue.body or "(no description)",
        grounding=grounding,
    )


def run_candidate(engine: Engine, issue: Issue, prompt: str, root: Path,
                  base: str, worktree: Path, runner: Runner, *,
                  commit: bool = True) -> Candidate:
    slug = slugify(issue.title)
    num = issue.number if issue.number is not None else "x"
    branch = f"vouch-dual/{num}-{slug}-{engine.name}"
    cand = Candidate(engine=engine.name, branch=branch, worktree=worktree)

    add = runner.run(["git", "-C", str(root), "worktree", "add", "-b", branch,
                      str(worktree), base])
    if add.code != 0:
        cand.error = f"worktree add failed: {add.stderr.strip()[:200]}"
        return cand

    try:
        engine.fix(cwd=str(worktree), prompt=prompt)
    except Exception as exc:
        cand.error = f"engine failed: {exc}"
        return cand

    # intent-to-add so a fix that only *creates* files still shows in `diff HEAD`.
    runner.run(["git", "-C", str(worktree), "add", "-A", "-N"])
    diff = runner.run(["git", "-C", str(worktree), "diff", "HEAD"]).stdout
    if not diff.strip():
        cand.error = "engine produced no diff"
        return cand
    cand.diff = diff

    if commit:
        runner.run(["git", "-C", str(worktree), "add", "-A"])
        title = f"resolve #{issue.number}: {issue.title}" if issue.number \
            else f"resolve: {issue.title}"
        c = runner.run(["git", "-C", str(worktree), "commit", "-m", title])
        if c.code != 0:
            cand.error = f"commit failed: {c.stderr.strip()[:200]}"
            return cand
        cand.sha = runner.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"]).stdout.strip()

    cand.ok = True
    return cand


_SUMMARY_PROMPT = (
    "in at most two lines, summarise the change you just made.\n"
    "line 1 must start with `ROOT CAUSE:` then one sentence.\n"
    "line 2 must start with `FIX:` then one sentence on the fix pattern."
)

# common typographic characters -> ascii, applied before the catch-all below.
# keys use \u escapes so ruff (RUF001) doesn't flag ambiguous glyphs in source.
_PUNCT = {
    "\u2014": "--", "\u2013": "-",      # em dash, en dash
    "\u2018": "'", "\u2019": "'",       # single curly quotes
    "\u201c": '"', "\u201d": '"',       # double curly quotes
    "\u2026": "...",                       # ellipsis
}


def _ascii(text: str) -> str:
    """Coerce text to ascii before it becomes a KB claim.

    storage writes proposals as yaml using the locale default encoding
    (latin-1 in some environments), so an untrusted issue title or engine
    summary carrying a character outside that range would raise
    UnicodeEncodeError mid-write and leave a zero-byte, KB-poisoning proposal.
    claim text is a derived summary; the verbatim original is preserved in the
    Source content, which is written as bytes.
    """
    for raw, rep in _PUNCT.items():
        text = text.replace(raw, rep)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def parse_summary(text: str) -> tuple[str, str]:
    root_cause, fix = "", ""
    for raw in text.splitlines():
        line = raw.strip()
        up = line.upper()
        if up.startswith("ROOT CAUSE") and ":" in line:
            root_cause = line.split(":", 1)[1].strip()
        elif up.startswith("FIX") and ":" in line:
            fix = line.split(":", 1)[1].strip()
    return root_cause, fix


def record_to_kb(store: KBStore, issue: Issue, chosen: Candidate, engine: Engine,
                 reason: str, *, proposed_by: str) -> list[str]:
    n = issue.number if issue.number is not None else "?"
    # the winning commit becomes a Source so every claim cites real evidence.
    content = (
        f"dual-solve winner ({chosen.engine}) for issue #{n}: {issue.title}\n"
        f"commit {chosen.sha or '(uncommitted)'}\n\n{chosen.diff}"
    ).encode()
    src = store.put_source(
        content,
        title=f"dual-solve patch for #{n} ({chosen.engine})",
        url=issue.url,
        locator=chosen.sha or chosen.branch,
        source_type=SourceType.COMMIT,
        media_type="text/x-diff",
    )

    root_cause, fix = parse_summary(
        engine.ask(cwd=str(chosen.worktree), prompt=_SUMMARY_PROMPT)
    )

    decision = f"for issue #{n} ({issue.title}), chose {chosen.engine}'s solution"
    decision += f" -- {reason}" if reason.strip() else "."
    drafts: list[tuple[str, str, float]] = [(decision, "decision", 0.8)]
    if root_cause:
        drafts.append(
            (f"root cause of issue #{n} ({issue.title}): {root_cause}", "fact", 0.7))
    if fix:
        drafts.append(
            (f"fix pattern for issue #{n} ({issue.title}): {fix}", "workflow", 0.7))

    ids: list[str] = []
    for text, ctype, conf in drafts[:3]:
        res = propose_claim(
            store, text=_ascii(text), evidence=[src.id], proposed_by=proposed_by,
            claim_type=ctype, confidence=conf,
            tags=["dual-solve", f"issue-{n}"],
            rationale=f"recorded by vouch dual-solve; winner={chosen.engine}",
        )
        ids.append(res.id)
    return ids


def cleanup(root: Path, candidates: list[Candidate], keep_branches: set[str],
            runner: Runner) -> None:
    for c in candidates:
        runner.run(["git", "-C", str(root), "worktree", "remove", "--force",
                    str(c.worktree)])
        if c.branch not in keep_branches:
            runner.run(["git", "-C", str(root), "branch", "-D", c.branch])


def _fmt_dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def prepare(store: KBStore, issue_ref: str, root: Path, runner: Runner, *,
            claude_effort: str = "high", codex_effort: str = "high",
            autonomy: str = "edit", dry_run: bool = False,
            workdir: Path | None = None,
            on_progress: Callable[[str], None] | None = None
            ) -> tuple[Issue, list[Candidate], dict[str, Engine]]:
    def report(msg: str) -> None:
        # both engine runs are multi-minute and silent; surface phase progress
        # so the operator knows which engine is active and that it started.
        if on_progress is not None:
            on_progress(msg)

    full = autonomy == "full"
    engines: dict[str, Engine] = {
        "claude": Engine("claude", claude_effort, runner, full_autonomy=full),
        "codex": Engine("codex", codex_effort, runner, full_autonomy=full),
    }
    report(f"fetching issue {issue_ref}")
    issue = fetch_issue(issue_ref, runner)
    report(f"issue #{issue.number}: {issue.title}" if issue.number is not None
           else f"issue: {issue.title}")
    report("grounding from the knowledge base")
    grounding = ground_prompt(store, f"{issue.title}\n{issue.body}")
    prompt = build_prompt(issue, grounding)
    wd = workdir if workdir is not None else Path(
        tempfile.mkdtemp(prefix="vouch-dual-"))
    efforts = {"claude": claude_effort, "codex": codex_effort}
    candidates: list[Candidate] = []
    for name in ("claude", "codex"):
        report(f"running {name} (effort={efforts[name]}); "
               "this can take a few minutes")
        start = time.monotonic()
        cand = run_candidate(
            engines[name], issue, prompt, root, "HEAD", wd / name, runner,
            commit=not dry_run,
        )
        dur = _fmt_dur(time.monotonic() - start)
        if cand.ok:
            report(f"{name}: produced a diff "
                   f"({len(cand.diff.splitlines())} lines) in {dur}")
        else:
            report(f"{name}: {cand.error or 'failed'} (after {dur})")
        candidates.append(cand)
    return issue, candidates, engines


def finalize(store: KBStore, root: Path, issue: Issue, chosen: Candidate | None,
             engines: dict[str, Engine], candidates: list[Candidate], reason: str,
             runner: Runner, *, record: bool, proposed_by: str) -> list[str]:
    ids: list[str] = []
    if chosen is not None and record:
        ids = record_to_kb(store, issue, chosen, engines[chosen.engine], reason,
                           proposed_by=proposed_by)
    keep = {chosen.branch} if chosen is not None else set()
    cleanup(root, candidates, keep, runner)
    return ids
