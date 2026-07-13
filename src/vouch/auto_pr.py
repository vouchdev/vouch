"""vouch auto-pr: open N mergeable PRs against any github repo.

a sibling tool to the knowledge base -- it never writes to storage /
proposals / lifecycle / the audit log, and the review gate is untouched.
it clones (or forks) a target repo, learns its contribution norms (from
shipped guidance, else synthesized from merged PRs), sources work items
(open issues first, then agent-discovered improvements), and drives
claude/codex to fix each one -- alternating which engine fixes and which
reviews -- opening a PR only when the repo's own test gate is green and
the reviewing engine signs off.

the entire subprocess boundary (git / gh / claude / codex) is funnelled
through one injectable ``Runner`` so every stage is unit-testable against
a fake. nothing here imports the KB layer.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol


def _require_engines() -> None:
    """Fail fast (before any clone) if a required binary isn't on PATH."""
    missing = [b for b in ("git", "gh", "claude", "codex") if shutil.which(b) is None]
    if missing:
        raise RuntimeError(
            f"required CLI not on PATH: {', '.join(missing)} "
            "(auto-pr needs git, gh, and both engines claude and codex)"
        )

# --- effort mapping -------------------------------------------------------

EFFORT_LEVELS = ("low", "medium", "high", "max")

# aliases (not pinned ids) so the tool tracks the latest model in each tier
# and survives model-id churn; the claude CLI resolves "opus"/"sonnet"/"haiku".
_CLAUDE_MODEL = {
    "low": "haiku",
    "medium": "sonnet",
    "high": "opus",
    "max": "opus",
}
# codex caps reasoning effort at "high"; "max" maps onto it.
_CODEX_REASONING = {"low": "low", "medium": "medium", "high": "high", "max": "high"}


def claude_flags(effort: str) -> list[str]:
    if effort not in _CLAUDE_MODEL:
        raise ValueError(f"unknown effort level: {effort!r}")
    return ["--model", _CLAUDE_MODEL[effort]]


def codex_flags(effort: str) -> list[str]:
    if effort not in _CODEX_REASONING:
        raise ValueError(f"unknown effort level: {effort!r}")
    return ["-c", f"model_reasoning_effort={_CODEX_REASONING[effort]}"]


def slugify(title: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:maxlen].strip("-") or "change"


def parse_repo(url: str) -> str:
    """Normalize a github url / shorthand to ``owner/name``."""
    u = url.strip().removesuffix(".git")
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)/?$", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    if re.fullmatch(r"[^/\s]+/[^/\s]+", u):
        return u
    raise ValueError(f"cannot parse github repo from {url!r}")


# --- subprocess boundary --------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> RunResult: ...


class SubprocessRunner:
    """Production ``Runner``: shells out and captures stdout/stderr as text."""

    def run(self, argv: list[str], *, cwd: str | None = None,
            stdin: str | None = None, timeout: int | None = None) -> RunResult:
        try:
            proc = subprocess.run(
                argv, cwd=cwd, input=stdin, capture_output=True, text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            # a missing binary becomes a nonzero result, not a crashed batch.
            return RunResult(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            return RunResult(124, out, f"timed out after {timeout}s")
        return RunResult(proc.returncode, proc.stdout, proc.stderr)


# --- data model -----------------------------------------------------------

@dataclass(frozen=True)
class WorkItem:
    kind: str            # "issue" | "discovered"
    title: str
    body: str
    slug: str
    number: int | None = None
    url: str | None = None


@dataclass(frozen=True)
class ReviewVerdict:
    approved: bool
    notes: str


@dataclass
class PRResult:
    item: WorkItem
    status: str          # "opened" | "skipped"
    fixer: str
    verifier: str
    url: str | None = None
    reason: str | None = None
    rounds: int = 0


# --- engine adapter -------------------------------------------------------

def engine_text(name: str, stdout: str) -> str:
    """Extract the agent's text reply; claude ``-p --output-format json``
    wraps it in a ``result`` field, codex returns it raw."""
    if name == "claude":
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout
        if isinstance(obj, dict) and "result" in obj:
            return str(obj["result"])
    return stdout


def parse_verdict(text: str) -> ReviewVerdict:
    """First non-empty line must be ``APPROVE`` or ``REQUEST_CHANGES: <notes>``;
    anything else is treated conservatively as a rejection."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("APPROVE"):
            return ReviewVerdict(True, line)
        if upper.startswith("REQUEST_CHANGES"):
            notes = line.split(":", 1)[1].strip() if ":" in line else ""
            return ReviewVerdict(False, notes or "changes requested")
        return ReviewVerdict(False, text.strip())
    return ReviewVerdict(False, "empty review output")


@dataclass
class Engine:
    name: str            # "claude" | "codex"
    effort: str
    runner: Runner
    timeout: int | None = None
    full_autonomy: bool = False  # opt-in to unsandboxed command execution

    def _run(self, prompt: str, *, cwd: str, edit: bool) -> str:
        # reviewing/asking is read-only (claude `plan` / codex `read-only`).
        # fixing auto-accepts edits but stays constrained by default: claude
        # `acceptEdits` (no arbitrary Bash) / codex `workspace-write` (sandboxed
        # to the clone, no network). `full_autonomy` escalates claude to
        # `bypassPermissions` for repos whose fix genuinely needs to run
        # commands -- an explicit, per-run operator choice, not the default.
        if self.name == "claude":
            if not edit:
                mode = "plan"
            elif self.full_autonomy:
                mode = "bypassPermissions"
            else:
                mode = "acceptEdits"
            argv = ["claude", "-p", prompt, "--permission-mode", mode,
                    "--output-format", "json", *claude_flags(self.effort)]
        else:
            sandbox = "workspace-write" if edit else "read-only"
            argv = ["codex", "exec", prompt, "--sandbox", sandbox, "--cd", cwd,
                    *codex_flags(self.effort)]
        res = self.runner.run(argv, cwd=cwd, timeout=self.timeout)
        return engine_text(self.name, res.stdout)

    def fix(self, *, cwd: str, prompt: str) -> str:
        """Run the engine with write access; it edits files in ``cwd``."""
        return self._run(prompt, cwd=cwd, edit=True)

    def ask(self, *, cwd: str, prompt: str) -> str:
        """Run the engine read-only and return its text reply."""
        return self._run(prompt, cwd=cwd, edit=False)

    def review(self, *, cwd: str, diff: str, prompt: str) -> ReviewVerdict:
        full = (
            f"{prompt}\n\nrespond with `APPROVE` or `REQUEST_CHANGES: <notes>` "
            f"as the very first line.\n\n--- DIFF ---\n{diff}\n"
        )
        return parse_verdict(self.ask(cwd=cwd, prompt=full))


# --- workspace resolution -------------------------------------------------

@dataclass
class RepoCtx:
    repo: str
    url: str
    clone_dir: Path
    default_branch: str
    fork_owner: str | None = None


def resolve_workspace(url: str, workspace: str, runner: Runner, *,
                      fork_owner: str | None = None,
                      has_push: bool = False) -> RepoCtx:
    repo = parse_repo(url)
    clone_dir = Path(workspace)
    forked = False
    if not (clone_dir / ".git").exists():
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        if has_push:
            # `gh repo clone <repo> <dir>` -- dir is a positional, not a gitflag.
            res = runner.run(["gh", "repo", "clone", repo, str(clone_dir)])
        else:
            # for fork --clone, the dir goes to the underlying git clone after `--`.
            res = runner.run(["gh", "repo", "fork", repo, "--clone",
                              "--default-branch-only", "--", str(clone_dir)])
            forked = True
        if res.code != 0:
            raise RuntimeError(f"could not obtain workspace: {res.stderr}")
    res = runner.run(["git", "-C", str(clone_dir), "symbolic-ref", "--short",
                      "refs/remotes/origin/HEAD"])
    default_branch = res.stdout.strip().rsplit("/", 1)[-1]
    if not default_branch:
        # origin/HEAD isn't always set on a fresh clone; ask the api directly
        # rather than blindly assuming "main" (repos use master/trunk too).
        view = runner.run(["gh", "repo", "view", repo, "--json",
                           "defaultBranchRef", "-q", ".defaultBranchRef.name"])
        default_branch = view.stdout.strip() or "main"
    # qualify the PR head as <fork-owner>:<branch> only when *we* forked this
    # run -- for an existing clone we can't assume origin is the user's fork, so
    # leave fork_owner unset unless the caller passed it explicitly.
    if fork_owner is None and forked:
        who = runner.run(["gh", "api", "user", "--jq", ".login"])
        fork_owner = who.stdout.strip() or None
    return RepoCtx(repo, url, clone_dir, default_branch, fork_owner)


# --- contribution guidance ------------------------------------------------

GUIDANCE_FILES = (
    "CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
)

_BOOTSTRAP_PROMPT = (
    "you are documenting how to contribute a mergeable PR to the github repo "
    "{repo}. below are recent merged PRs as json (titles, bodies, urls). "
    "synthesize a concise contribution guide: branch naming, commit/PR-title "
    "conventions, how to run the test/build gate, PR-body format, and review "
    "norms. output github-flavored markdown only.\n\n{prs}"
)


def find_guidance(clone_dir: Path) -> list[Path]:
    found: list[Path] = []
    for rel in GUIDANCE_FILES:
        p = clone_dir / rel
        if p.exists():
            found.append(p)
    skills_dir = clone_dir / ".claude" / "skills"
    if skills_dir.exists():
        found.extend(sorted(skills_dir.glob("**/SKILL.md")))
    codex_dir = clone_dir / ".codex"
    if codex_dir.exists():
        found.extend(sorted(codex_dir.glob("**/*.md")))
    return found


def detect_or_bootstrap_guidance(ctx: RepoCtx, engine: Engine,
                                 runner: Runner) -> str:
    found = find_guidance(ctx.clone_dir)
    if found:
        return "\n\n".join(
            f"# {p.relative_to(ctx.clone_dir)}\n{p.read_text(errors='replace')}"
            for p in found[:6]
        )
    res = runner.run([
        "gh", "pr", "list", "--repo", ctx.repo, "--state", "merged",
        "--limit", "30", "--json", "title,body,url",
    ])
    prompt = _BOOTSTRAP_PROMPT.format(repo=ctx.repo, prs=res.stdout or "[]")
    guide = engine.ask(cwd=str(ctx.clone_dir), prompt=prompt) or "# contributing\n"
    skill = (ctx.clone_dir / ".claude" / "skills" / "auto-pr-contributing"
             / "SKILL.md")
    skill.parent.mkdir(parents=True, exist_ok=True)
    front = (
        "---\nname: auto-pr-contributing\ndescription: synthesized contribution "
        f"guide for {ctx.repo}, derived from merged PRs.\n---\n\n"
    )
    skill.write_text(front + guide, encoding="utf-8")
    codex_mirror = ctx.clone_dir / ".codex" / "auto-pr-contributing.md"
    codex_mirror.parent.mkdir(parents=True, exist_ok=True)
    codex_mirror.write_text(guide, encoding="utf-8")
    return guide


# --- work-item sourcing ---------------------------------------------------

_DISCOVER_PROMPT = (
    "find exactly ONE small, real, mergeable improvement in this repo "
    "(a genuine bug, a missing test, a doc error -- not a stylistic nitpick). "
    "respond with a single line: `<short imperative title>`, then a blank "
    "line, then a one-paragraph rationale. do not edit any files."
)


def _title_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def is_duplicate(ctx: RepoCtx, topic: str, runner: Runner) -> bool:
    res = runner.run([
        "gh", "pr", "list", "--repo", ctx.repo, "--state", "all",
        "--search", topic, "--limit", "20", "--json", "title,url",
    ])
    # fail *closed*: if we can't verify uniqueness (gh error / bad json), assume
    # a duplicate and skip rather than risk opening the Nth copy of a known PR.
    if res.code != 0:
        return True
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return True
    # token-overlap (jaccard), not substring: a generic existing PR titled
    # "fix" must not subsume every longer candidate, and vice versa.
    want = _title_tokens(topic)
    if not want:
        return False
    for row in rows:
        have = _title_tokens(str(row.get("title", "")))
        if have and len(want & have) / len(want | have) >= 0.6:
            return True
    return False


def open_issues(ctx: RepoCtx, runner: Runner, *,
                labels: tuple[str, ...] = ()) -> list[WorkItem]:
    argv = ["gh", "issue", "list", "--repo", ctx.repo, "--state", "open",
            "--search", "no:assignee sort:created-desc", "--limit", "50",
            "--json", "number,title,body,url"]
    for lab in labels:
        argv += ["--label", lab]
    res = runner.run(argv)
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        rows = []
    items: list[WorkItem] = []
    for row in rows:
        title = str(row.get("title", "")).strip()
        items.append(WorkItem(
            kind="issue", title=title, body=str(row.get("body", "")),
            slug=slugify(title), number=row.get("number"), url=row.get("url"),
        ))
    return items


def discover_items(ctx: RepoCtx, engine: Engine, n: int) -> list[WorkItem]:
    items: list[WorkItem] = []
    for _ in range(n):
        text = engine.ask(cwd=str(ctx.clone_dir), prompt=_DISCOVER_PROMPT).strip()
        if not text:
            continue
        title, _, body = text.partition("\n")
        title = title.strip().lstrip("#").strip().strip("`") or "improvement"
        items.append(WorkItem("discovered", title, body.strip(), slugify(title)))
    return items


def source_work_items(ctx: RepoCtx, count: int, runner: Runner,
                      fixer_engine: Engine, *,
                      labels: tuple[str, ...] = ()) -> list[WorkItem]:
    chosen: list[WorkItem] = []
    for it in open_issues(ctx, runner, labels=labels):
        if len(chosen) >= count:
            break
        if not is_duplicate(ctx, it.title, runner):
            chosen.append(it)
    if len(chosen) < count:
        for it in discover_items(ctx, fixer_engine, count - len(chosen)):
            if not is_duplicate(ctx, it.title, runner):
                chosen.append(it)
            if len(chosen) >= count:
                break
    # uniquify slugs so two items can't collide on the same branch name.
    seen: dict[str, int] = {}
    unique: list[WorkItem] = []
    for it in chosen[:count]:
        n = seen.get(it.slug, 0)
        seen[it.slug] = n + 1
        unique.append(it if n == 0 else replace(it, slug=f"{it.slug}-{n + 1}"))
    return unique


# --- local test gate ------------------------------------------------------

def detect_gate(clone_dir: Path) -> list[str] | None:
    mk = clone_dir / "Makefile"
    if mk.exists() and re.search(r"^check:", mk.read_text(errors="replace"),
                                 re.MULTILINE):
        return ["make", "check"]
    if (clone_dir / "pyproject.toml").exists() or (clone_dir / "setup.cfg").exists():
        return ["python", "-m", "pytest", "-q"]
    pkg = clone_dir / "package.json"
    if pkg.exists() and '"test"' in pkg.read_text(errors="replace"):
        return ["npm", "test", "--silent"]
    if (clone_dir / "Cargo.toml").exists():
        return ["cargo", "test"]
    if (clone_dir / "go.mod").exists():
        return ["go", "test", "./..."]
    return None


def run_gate(clone_dir: Path, runner: Runner) -> tuple[bool, str]:
    cmd = detect_gate(clone_dir)
    if cmd is None:
        return True, "no test gate detected; skipping (proceed with caution)"
    res = runner.run(cmd, cwd=str(clone_dir), timeout=1800)
    log = (res.stdout + "\n" + res.stderr).strip()[-4000:]
    return res.code == 0, log


# --- per-item processing --------------------------------------------------

_FIX_PROMPT = (
    "you are contributing a single mergeable PR to {repo}. work item:\n"
    "title: {title}\n{issue_ref}\nbody:\n{body}\n\n"
    "contribution guidance:\n{guidance}\n\n"
    "make the smallest correct change that resolves this, including a "
    "regression test if the repo has tests. do not add any AI-attribution "
    "trailer to commits. keep it to one logical change.{revise}"
)
_REVIEW_PROMPT = (
    "review this diff as a maintainer of {repo}. does it correctly and "
    "minimally resolve `{title}`, follow the repo's conventions, and include "
    "a test where appropriate? it must merge cleanly."
)


def reset_workspace(ctx: RepoCtx, runner: Runner) -> None:
    """Return the clone to a clean default branch so a skipped item's failed
    edits don't bleed into the next one. The workspace is treated as a
    throwaway working tree owned by auto-pr."""
    clone = str(ctx.clone_dir)
    runner.run(["git", "-C", clone, "switch", ctx.default_branch])
    runner.run(["git", "-C", clone, "reset", "--hard",
                f"origin/{ctx.default_branch}"])
    runner.run(["git", "-C", clone, "clean", "-fd"])


def git_branch(ctx: RepoCtx, slug: str, runner: Runner) -> str:
    # `switch -C` creates-or-resets, so a leftover branch from a prior run
    # (same slug) can't wedge the pipeline onto the wrong branch.
    branch = f"auto-pr/{slug}"
    runner.run(["git", "-C", str(ctx.clone_dir), "switch", "-C", branch,
                f"origin/{ctx.default_branch}"])
    return branch


def git_diff(ctx: RepoCtx, runner: Runner) -> str:
    # intent-to-add untracked files first, so a fix that *creates* a file (a new
    # test/module/doc -- the most common change) shows up in `git diff HEAD`
    # instead of being misread as "no diff".
    runner.run(["git", "-C", str(ctx.clone_dir), "add", "-A", "-N"])
    res = runner.run(["git", "-C", str(ctx.clone_dir), "diff", "HEAD"])
    return res.stdout


def commit_all(ctx: RepoCtx, title: str, runner: Runner) -> None:
    runner.run(["git", "-C", str(ctx.clone_dir), "add", "-A"])
    runner.run(["git", "-C", str(ctx.clone_dir), "commit", "-m", title])


def open_pr(ctx: RepoCtx, item: WorkItem, branch: str, runner: Runner) -> str:
    """Push the branch and open the PR. Raises RuntimeError on any failure so
    the caller never reports a PR that wasn't actually created."""
    closes = f"\n\ncloses #{item.number}" if item.number else ""
    body = (f"{item.body.strip()[:600]}{closes}").strip() or "see linked issue."
    push = runner.run(["git", "-C", str(ctx.clone_dir), "push", "-u", "origin",
                       branch])
    if push.code != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()[:300]}")
    head = f"{ctx.fork_owner}:{branch}" if ctx.fork_owner else branch
    res = runner.run([
        "gh", "pr", "create", "--repo", ctx.repo, "--head", head,
        "--base", ctx.default_branch, "--title", item.title, "--body", body,
    ], cwd=str(ctx.clone_dir))
    if res.code != 0:
        raise RuntimeError(f"gh pr create failed: {res.stderr.strip()[:300]}")
    out = res.stdout.strip()
    url = out.splitlines()[-1] if out else ""
    if not url:
        raise RuntimeError("gh pr create produced no url")
    return url


def process_item(ctx: RepoCtx, item: WorkItem, fixer: Engine, verifier: Engine,
                 runner: Runner, guidance: str, *, max_revise: int = 2,
                 dry_run: bool = False) -> PRResult:
    result = PRResult(item=item, status="skipped", fixer=fixer.name,
                      verifier=verifier.name)
    reset_workspace(ctx, runner)
    branch_slug = f"{item.number}-{item.slug}" if item.number else item.slug
    branch = git_branch(ctx, branch_slug, runner)
    issue_ref = f"issue: {item.url}" if item.url else "(no tracked issue)"
    revise_note = ""
    for attempt in range(max_revise + 1):
        result.rounds = attempt + 1
        prompt = _FIX_PROMPT.format(
            repo=ctx.repo, title=item.title, issue_ref=issue_ref,
            body=item.body, guidance=guidance[:4000], revise=revise_note,
        )
        fixer.fix(cwd=str(ctx.clone_dir), prompt=prompt)
        if not git_diff(ctx, runner).strip():
            result.reason = "fixer produced no diff"
            return result
        gate_ok, gate_log = run_gate(ctx.clone_dir, runner)
        if not gate_ok:
            revise_note = f"\n\nthe test gate FAILED:\n{gate_log[-1500:]}\nfix it."
            continue
        # re-capture AFTER the gate so the reviewed diff equals what gets
        # committed -- a gate with autofix/codegen can mutate the tree.
        diff = git_diff(ctx, runner)
        verdict = verifier.review(
            cwd=str(ctx.clone_dir), diff=diff,
            prompt=_REVIEW_PROMPT.format(repo=ctx.repo, title=item.title),
        )
        if verdict.approved:
            commit_all(ctx, item.title, runner)
            if dry_run:
                result.status = "opened"
                result.reason = "dry-run (not pushed)"
                return result
            try:
                result.url = open_pr(ctx, item, branch, runner)
            except RuntimeError as e:
                result.reason = f"pr creation failed: {e}"
                return result
            result.status = "opened"
            result.reason = verdict.notes
            return result
        revise_note = f"\n\nreviewer requested changes:\n{verdict.notes}"
    result.reason = f"failed verification after {max_revise + 1} rounds"
    return result


# --- orchestrator ---------------------------------------------------------

def run_auto_pr(repo_url: str, workspace: str, count: int,
                claude_effort: str, codex_effort: str, *,
                runner: Runner | None = None,
                labels: tuple[str, ...] = (),
                fork_owner: str | None = None,
                max_revise: int = 2,
                autonomy: str = "edit",
                dry_run: bool = False) -> list[PRResult]:
    if runner is None:
        # only enforce the PATH check for real runs; tests inject a fake.
        _require_engines()
        runner = SubprocessRunner()
    full = autonomy == "full"
    claude = Engine("claude", claude_effort, runner, full_autonomy=full)
    codex = Engine("codex", codex_effort, runner, full_autonomy=full)
    engines = (claude, codex)

    ctx = resolve_workspace(repo_url, workspace, runner, fork_owner=fork_owner)
    guidance = detect_or_bootstrap_guidance(ctx, claude, runner)
    items = source_work_items(ctx, count, runner, claude, labels=labels)

    results: list[PRResult] = []
    for i, item in enumerate(items):
        fixer = engines[i % 2]
        verifier = engines[(i + 1) % 2]
        results.append(process_item(
            ctx, item, fixer, verifier, runner, guidance,
            max_revise=max_revise, dry_run=dry_run,
        ))
    return results
