# vouch dual-solve Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `vouch dual-solve <issue-url>` CLI command that runs Claude Code and Codex on one GitHub issue in isolated git worktrees, lets the operator pick the winning diff, keeps that branch, and proposes the chosen solution's rationale into the KB through the gated proposal flow.

**Architecture:** A new `src/vouch/dual_solve.py` orchestration module plus one thin click command in `src/vouch/cli.py`, mirroring the existing `auto_pr.py` + `auto_pr_cmd` split. The subprocess boundary (git / gh / claude / codex) is funnelled through `auto_pr`'s injectable `Runner`, so every stage is unit-testable against a `FakeRunner`. `dual_solve` reuses `Engine`, `Runner`, `SubprocessRunner`, and `slugify` from `auto_pr` (import, not re-extract — see Global Constraints), and unlike `auto_pr` it *does* call the KB layer, but only via `proposals.propose_claim` so the review gate is preserved.

**Tech Stack:** Python 3, click, pytest, mypy, ruff. Reuses `vouch.auto_pr`, `vouch.storage.KBStore`, `vouch.proposals.propose_claim`, `vouch.context.build_context_pack`, `vouch.models`.

## Global Constraints

- This is a **CLI-only sibling tool**, exactly like `auto-pr`. It is NOT a `kb.*` method: do **not** add it to `server.py`, `jsonl_server.py`, `capabilities.py`, or `openclaw.plugin.json`. `test_capabilities` does not apply.
- **Review gate is sacred.** The only KB writes are `store.put_source(...)` (source intake — ungated by design) and `proposals.propose_claim(...)` (lands in `proposed/`). Never call `proposals.approve` or any direct durable-claim write. Nothing is auto-approved.
- **Reuse, don't re-extract.** Import `Engine, Runner, SubprocessRunner, slugify` from `vouch.auto_pr`. Do NOT move them into a new `_engines.py` — `tests/test_auto_pr.py` monkeypatches `ap.shutil.which` and depends on those symbols living in `auto_pr`, so extraction is disruptive and out of scope.
- **Import where first used — do NOT front-load.** ruff `F401` (unused import) is part of the gate and is enforced at *every* commit, not just the final one. Each task adds `from .X import Y` (and `import json` / `import tempfile`) only when its own code references the symbol; the task's **Interfaces → Consumes** block names the source module for every symbol it needs. Keep `__all__` isort-sorted (ruff `RUF022`) when a task adds a public name. Earlier tasks therefore import less than the full surface: e.g. `build_context_pack`/`ContextPack` arrive with Task 3, `Engine`/`Runner`/`slugify` with Task 4, `SourceType`/`propose_claim` with Task 5, `tempfile`/`SubprocessRunner` with Task 6.
- **Conventional commits**, lowercase summary ≤72 chars, lowercase body. **No `Co-Authored-By` trailer.** Types: `feat | fix | refactor | test | docs | chore`.
- **Stage by name** in every commit step — never `git add -A` / `git add .` (it leaks `.claude/`, `web/`, local scratch).
- **CI gate** that must stay green: `.venv/bin/pytest tests/ -q --ignore=tests/embeddings && .venv/bin/mypy src && .venv/bin/ruff check src tests` (equivalent to `make check`).
- **Run tools via the venv entry points** — `.venv/bin/pytest`, `.venv/bin/mypy`, `.venv/bin/ruff` — never the `python -m <module>` form. A local PreToolUse hook mis-parses the `-m` in `python -m pytest` as a commit `-m` flag and blocks the command; the entry points avoid it.
- All new functions carry type annotations (mypy `src` is strict and is the gate most often missed).
- Branch off `test` (the active integration branch) before starting: `git switch -c feat/dual-solve test`. NOTE: `origin/main` is 211 commits behind and does NOT contain `auto_pr.py`, which this plan imports from — `test` is the only viable base. (The repo's usual `origin/main` ship-flow doesn't apply until `test` merges down.)

---

### Task 1: Module scaffold — data model, `_require_engines`, `parse_issue_ref`

**Files:**
- Create: `src/vouch/dual_solve.py`
- Create: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `Engine, Runner, SubprocessRunner, slugify` from `vouch.auto_pr`.
- Produces:
  - `Issue(title: str, body: str, number: int | None = None, url: str | None = None)` — frozen dataclass.
  - `Candidate(engine: str, branch: str, worktree: Path, diff: str = "", sha: str = "", ok: bool = False, error: str | None = None)` — mutable dataclass.
  - `_require_engines() -> None`
  - `parse_issue_ref(ref: str) -> tuple[str | None, str]` — returns `(repo_or_None, gh_locator)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py
"""tests for vouch dual-solve.

every test runs against a FakeRunner: no network, no real claude/codex/gh.
only the subprocess boundary is mocked; all stage logic is real.
"""
from __future__ import annotations

from pathlib import Path

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.dual_solve'`.

- [ ] **Step 3: Write the module scaffold**

```python
# src/vouch/dual_solve.py
"""vouch dual-solve: run claude + codex on one issue; the operator picks a winner.

a sibling tool in the spirit of auto_pr -- it orchestrates the two coding
engines through the same injectable Runner so every stage is unit-testable
against a fake. unlike auto_pr it DOES touch the KB, but only ever via
``proposals.propose_claim`` (writes land in ``proposed/``), so the review gate
is preserved: nothing is auto-approved.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Candidate", "Issue", "parse_issue_ref"]


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): scaffold module, issue-ref parsing, engine check"
```

---

### Task 2: `fetch_issue`

**Files:**
- Modify: `src/vouch/dual_solve.py`
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `parse_issue_ref`, `Issue`, `Runner` (Task 1).
- Produces: `fetch_issue(ref: str, runner: Runner) -> Issue`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py  (append)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k fetch_issue -q`
Expected: FAIL with `AttributeError: module 'vouch.dual_solve' has no attribute 'fetch_issue'`.

- [ ] **Step 3: Add `fetch_issue`**

```python
# src/vouch/dual_solve.py  (append; also add "fetch_issue" to __all__)
def fetch_issue(ref: str, runner: Runner) -> Issue:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k fetch_issue -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): fetch issue title/body via gh"
```

---

### Task 3: `repo_root`, `ground_prompt`, `build_prompt`

**Files:**
- Modify: `src/vouch/dual_solve.py`
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `Issue` (Task 1); `build_context_pack`, `ContextPack`, `KBStore`.
- Produces:
  - `repo_root(runner: Runner, cwd: Path) -> Path`
  - `ground_prompt(store: KBStore, query: str, *, limit: int = 8) -> str`
  - `build_prompt(issue: Issue, grounding: str) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py  (append)
from vouch.models import ContextItem, ContextPack
from vouch.storage import KBStore


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "repo_root or ground or build_prompt" -q`
Expected: FAIL — missing attributes `repo_root` / `ground_prompt` / `build_prompt`.

- [ ] **Step 3: Add the three functions**

```python
# src/vouch/dual_solve.py  (append; add the three names to __all__)
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
    res = runner.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if res.code != 0:
        raise RuntimeError("not inside a git repository")
    return Path(res.stdout.strip())


def ground_prompt(store: KBStore, query: str, *, limit: int = 8) -> str:
    pack = build_context_pack(store, query=query, limit=limit)
    items = pack.items if isinstance(pack, ContextPack) else []
    if not items:
        return "(the knowledge base has nothing on this topic yet.)"
    return "\n".join(f"- [{it.id}] {it.summary}" for it in items)


def build_prompt(issue: Issue, grounding: str) -> str:
    return _FIX_PROMPT.format(
        num=issue.number if issue.number is not None else "?",
        title=issue.title,
        body=issue.body or "(no description)",
        grounding=grounding,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "repo_root or ground or build_prompt" -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): repo-root, kb grounding, shared fix prompt"
```

---

### Task 4: `run_candidate`

**Files:**
- Modify: `src/vouch/dual_solve.py`
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `Engine`, `Issue`, `Candidate`, `slugify`, `Runner`.
- Produces: `run_candidate(engine: Engine, issue: Issue, prompt: str, root: Path, base: str, worktree: Path, runner: Runner, *, commit: bool = True) -> Candidate`.

  Branch name: `vouch-dual/<number-or-x>-<slug>-<engine>`. Sets `ok=True` only when a non-empty diff was produced (and, when `commit=True`, committed). On any failure sets `ok=False` and a human-readable `error`; never raises.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py  (append)
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


def test_run_candidate_dry_run_skips_commit(tmp_path):
    root, wt = tmp_path, tmp_path / "wt"
    fr = FakeRunner([(["git", "-C", str(wt), "diff", "HEAD"],
                      ap.RunResult(0, "patch", ""))])
    cand = ds.run_candidate(ds.Engine("codex", "high", fr), _issue(),
                            "p", root, "HEAD", wt, fr, commit=False)
    assert cand.ok is True and cand.diff == "patch" and cand.sha == ""
    assert not any(c[:4] == ["git", "-C", str(wt), "commit"] for c in fr.calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k run_candidate -q`
Expected: FAIL — `module 'vouch.dual_solve' has no attribute 'run_candidate'`.

- [ ] **Step 3: Add `run_candidate`**

```python
# src/vouch/dual_solve.py  (append; add "run_candidate" to __all__)
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

    engine.fix(cwd=str(worktree), prompt=prompt)

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k run_candidate -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): run one engine in an isolated worktree"
```

---

### Task 5: `parse_summary` + `record_to_kb`

**Files:**
- Modify: `src/vouch/dual_solve.py`
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `Issue`, `Candidate`, `Engine`, `KBStore`, `SourceType`, `propose_claim`.
- Produces:
  - `parse_summary(text: str) -> tuple[str, str]` — `(root_cause, fix_pattern)`, each may be `""`.
  - `record_to_kb(store: KBStore, issue: Issue, chosen: Candidate, engine: Engine, reason: str, *, proposed_by: str) -> list[str]` — registers the winning commit as a `Source`, asks `engine` (read-only) for a one-shot root-cause/fix summary, proposes ≤3 claims citing the source, returns the proposal ids.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py  (append)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "parse_summary or record_to_kb" -q`
Expected: FAIL — missing `parse_summary` / `record_to_kb`.

- [ ] **Step 3: Add `parse_summary` and `record_to_kb`**

```python
# src/vouch/dual_solve.py  (append; add both names to __all__)
_SUMMARY_PROMPT = (
    "in at most two lines, summarise the change you just made.\n"
    "line 1 must start with `ROOT CAUSE:` then one sentence.\n"
    "line 2 must start with `FIX:` then one sentence on the fix pattern."
)


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
    # claim text is written to a yaml file via storage.py, which encodes with
    # the locale default (latin-1 here) -- keep it ascii (no em dash) or the
    # write raises UnicodeEncodeError (same pre-existing bug as test_volunteer_context).
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
            store, text=text, evidence=[src.id], proposed_by=proposed_by,
            claim_type=ctype, confidence=conf,
            tags=["dual-solve", f"issue-{n}"],
            rationale=f"recorded by vouch dual-solve; winner={chosen.engine}",
        )
        ids.append(res.id)
    return ids
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "parse_summary or record_to_kb" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): record the winning solution as gated proposals"
```

---

### Task 6: `cleanup`, `prepare`, `finalize`

**Files:**
- Modify: `src/vouch/dual_solve.py`
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5; `Engine`, `SubprocessRunner`, `tempfile`.
- Produces:
  - `cleanup(root: Path, candidates: list[Candidate], keep_branches: set[str], runner: Runner) -> None` — removes every candidate worktree (force) and deletes every candidate branch not in `keep_branches`.
  - `prepare(store: KBStore, issue_ref: str, root: Path, runner: Runner, *, claude_effort: str = "high", codex_effort: str = "high", autonomy: str = "edit", dry_run: bool = False, workdir: Path | None = None) -> tuple[Issue, list[Candidate], dict[str, Engine]]` — fetches the issue, grounds the prompt, runs both engines (claude then codex) in sibling worktrees, returns the issue, both candidates, and the engines keyed by name. No KB write, no interaction.
  - `finalize(store: KBStore, root: Path, issue: Issue, chosen: Candidate | None, engines: dict[str, Engine], candidates: list[Candidate], reason: str, runner: Runner, *, record: bool, proposed_by: str) -> list[str]` — if `chosen` and `record`, record to KB; then clean up (keeping only the chosen branch); returns proposal ids (empty when not recording).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_dual_solve.py  (append)
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
    issue, cands, engines = ds.prepare(store, "o/n#1", tmp_path, FakeRunner(),
                                       workdir=tmp_path / "wd")
    assert seen == ["claude", "codex"]
    assert [c.engine for c in cands] == ["claude", "codex"]
    assert set(engines) == {"claude", "codex"}


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "cleanup or prepare or finalize" -q`
Expected: FAIL — missing `cleanup` / `prepare` / `finalize`.

- [ ] **Step 3: Add `cleanup`, `prepare`, `finalize`**

```python
# src/vouch/dual_solve.py  (append; add the three names to __all__)
def cleanup(root: Path, candidates: list[Candidate], keep_branches: set[str],
            runner: Runner) -> None:
    for c in candidates:
        runner.run(["git", "-C", str(root), "worktree", "remove", "--force",
                    str(c.worktree)])
        if c.branch not in keep_branches:
            runner.run(["git", "-C", str(root), "branch", "-D", c.branch])


def prepare(store: KBStore, issue_ref: str, root: Path, runner: Runner, *,
            claude_effort: str = "high", codex_effort: str = "high",
            autonomy: str = "edit", dry_run: bool = False,
            workdir: Path | None = None
            ) -> tuple[Issue, list[Candidate], dict[str, Engine]]:
    full = autonomy == "full"
    engines: dict[str, Engine] = {
        "claude": Engine("claude", claude_effort, runner, full_autonomy=full),
        "codex": Engine("codex", codex_effort, runner, full_autonomy=full),
    }
    issue = fetch_issue(issue_ref, runner)
    grounding = ground_prompt(store, f"{issue.title}\n{issue.body}")
    prompt = build_prompt(issue, grounding)
    wd = workdir if workdir is not None else Path(
        tempfile.mkdtemp(prefix="vouch-dual-"))
    candidates: list[Candidate] = []
    for name in ("claude", "codex"):
        candidates.append(run_candidate(
            engines[name], issue, prompt, root, "HEAD", wd / name, runner,
            commit=not dry_run,
        ))
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k "cleanup or prepare or finalize" -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/dual_solve.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): orchestrate both engines, finalize the winner"
```

---

### Task 7: CLI command `vouch dual-solve`

**Files:**
- Modify: `src/vouch/cli.py` (add the command near `auto_pr_cmd`, ~line 1965)
- Modify: `tests/test_dual_solve.py`

**Interfaces:**
- Consumes: `dual_solve.prepare`, `dual_solve.finalize`, `dual_solve._require_engines`, `dual_solve.repo_root`, `dual_solve.SubprocessRunner`; CLI helpers `_load_store`, `_whoami`, `_emit_json`.
- Produces: a `dual-solve` click command. Interactive flow: show each candidate's `--stat`-style header + diff, then prompt `[c]laude / [x]codex / [n]either`. `--json` skips the prompt and emits both diffs + metadata (keeping both successful branches). Failure handling: if exactly one candidate is `ok`, ask whether to proceed with the survivor or abort; if none, abort non-zero.

- [ ] **Step 1: Write the failing tests (CliRunner, fully mocked engine layer)**

```python
# tests/test_dual_solve.py  (append)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k cli_dual_solve -q`
Expected: FAIL — `No such command 'dual-solve'`.

- [ ] **Step 3: Add the CLI command in `src/vouch/cli.py`**

Insert after `auto_pr_cmd` (before the `# --- sync ---` banner, ~line 1965):

```python
# --- dual-solve: run claude + codex on one issue; operator picks a winner ---


@cli.command(name="dual-solve")
@click.argument("issue_url")
@click.option("--claude-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--codex-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--autonomy", default="edit", show_default=True,
              type=click.Choice(["edit", "full"]),
              help="'edit' auto-accepts file edits only (safer default); "
                   "'full' lets engines run arbitrary commands.")
@click.option("--reason", default=None,
              help="why you picked the winner (skips the interactive prompt).")
@click.option("--no-record", is_flag=True,
              help="keep the chosen branch but propose nothing to the kb.")
@click.option("--dry-run", is_flag=True,
              help="run both engines but make no commits / kb writes.")
@click.option("--json", "as_json", is_flag=True,
              help="non-interactive: emit both diffs + metadata, no prompt.")
def dual_solve_cmd(issue_url: str, claude_effort: str, codex_effort: str,
                   autonomy: str, reason: str | None, no_record: bool,
                   dry_run: bool, as_json: bool) -> None:
    """Run claude + codex on ISSUE_URL; you pick the winning diff.

    Each engine works in its own git worktree on a fresh branch. You compare
    the two diffs, keep one branch, and (unless --no-record) the rationale is
    proposed into the kb for review. A sibling tool to auto-pr; the review
    gate is untouched — nothing is auto-approved.
    """
    from . import dual_solve as ds_mod
    from .auto_pr import SubprocessRunner  # lives in auto_pr, not dual_solve
    store = _load_store()
    runner = SubprocessRunner()
    try:
        ds_mod._require_engines()
        root = ds_mod.repo_root(runner, Path.cwd())
        issue, candidates, engines = ds_mod.prepare(
            store, issue_url, root, runner,
            claude_effort=claude_effort, codex_effort=codex_effort,
            autonomy=autonomy, dry_run=dry_run,
        )
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        _emit_json({
            "issue": {"number": issue.number, "title": issue.title,
                      "url": issue.url},
            "candidates": [
                {"engine": c.engine, "branch": c.branch, "ok": c.ok,
                 "error": c.error, "diff": c.diff} for c in candidates
            ],
        })
        return

    for c in candidates:
        click.echo(f"\n=== {c.engine} ({c.branch}) ===", err=True)
        if c.ok:
            click.echo(c.diff)
        else:
            click.echo(f"(failed: {c.error})", err=True)

    ok = [c for c in candidates if c.ok]
    if not ok:
        raise click.ClickException("both engines failed; nothing to choose")
    choice: str | None  # the [n]either branch assigns None; mypy needs the union
    if len(ok) == 1:
        survivor = ok[0]
        if not click.confirm(
                f"only {survivor.engine} produced a usable diff; proceed with it?",
                default=True):
            ds_mod.finalize(store, root, issue, None, engines, candidates, "",
                            runner, record=False, proposed_by=_whoami())
            raise click.ClickException("aborted; both branches discarded")
        choice = survivor.engine
    else:
        letter = click.prompt("pick a winner [c]laude / [x]codex / [n]either",
                              type=click.Choice(["c", "x", "n"]), default="c")
        choice = {"c": "claude", "x": "codex", "n": None}[letter]

    chosen = next((c for c in candidates if c.engine == choice), None)
    if reason is None and chosen is not None and not no_record and not dry_run:
        reason = click.prompt("one line: why this solution", default="")

    ids = ds_mod.finalize(
        store, root, issue, chosen, engines, candidates, reason or "", runner,
        record=not no_record and not dry_run, proposed_by=_whoami(),
    )
    if chosen is None:
        click.echo("kept neither; both branches discarded", err=True)
        return
    click.echo(f"kept {chosen.branch}", err=True)
    for pid in ids:
        click.echo(f"proposed {pid} -- review with `vouch approve {pid}`", err=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dual_solve.py -k cli_dual_solve -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/vouch/cli.py tests/test_dual_solve.py
git commit -m "feat(dual-solve): add the dual-solve cli command"
```

---

### Task 8: Changelog + full gate green

**Files:**
- Modify: `CHANGELOG.md`
- (verify only) all of the above

**Interfaces:** none (documentation + gate).

- [ ] **Step 1: Add a changelog entry**

Open `CHANGELOG.md`. Under the `## [Unreleased]` heading (add an `### Added` subsection if the file's existing style uses them; otherwise match whatever bullet style is already there), add:

```markdown
- `vouch dual-solve <issue-url>`: run claude + codex on one github issue in
  isolated worktrees, pick the winning diff, and propose the chosen solution's
  rationale into the kb (review-gated; nothing auto-approved). a sibling tool
  to `auto-pr`.
```

- [ ] **Step 2: Run the full suite for the new module**

Run: `.venv/bin/pytest tests/test_dual_solve.py tests/test_auto_pr.py -q`
Expected: PASS — the dual-solve suite is green and `auto_pr` is unaffected (no symbols were moved).

- [ ] **Step 3: Run the whole CI gate**

Run: `.venv/bin/pytest tests/ -q --ignore=tests/embeddings && .venv/bin/mypy src && .venv/bin/ruff check src tests`
Expected: all green. Imports were added per-task as symbols came into use (see Global Constraints), so `ruff` should report no `F401`; if it flags a leftover unused import in `dual_solve.py`, the task that was meant to use it skipped it — investigate rather than blindly deleting. If `mypy` flags `build_context_pack`'s `ContextPack | dict` union, confirm the `isinstance(pack, ContextPack)` guard in `ground_prompt` is present (it narrows the type).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): note vouch dual-solve"
```

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/dual-solve
```

PR body (lowercase, no `Co-Authored-By` trailer) must call out the one review-worthy point: **this is the first sibling tool that writes to the kb.** State plainly that it only ever *proposes* (writes land in `proposed/`, approval still requires a human `vouch approve`), so the review-gate invariant is preserved — `auto-pr`'s "never writes to the kb" rule is widened, not broken.

---

## Self-Review

**Spec coverage:**
- Real `vouch` CLI subcommand → Task 7. ✓
- Worktree + branch + diff execution → Task 4 (`run_candidate`), Task 6 (`prepare`). ✓
- KB record: decision + ≤3 approach claims, gated → Task 5 (`record_to_kb`, `propose_claim` only). ✓
- Grounding: inject identical `vouch context` into both prompts → Task 3 (`ground_prompt`) + Task 6 (one shared `prompt` passed to both engines). ✓
- Failure handling: prompt on single failure, abort on double → Task 7 CLI (`click.confirm` survivor path; `both engines failed` abort). ✓
- Competitors independent (no cross-critique) → `prepare` runs each engine on the same prompt with no review step. ✓
- Invariants (review gate, storage purity, citations enforced) → Global Constraints + Task 5 registers the commit `Source` before proposing, so every claim cites evidence. ✓
- Testing list from spec → Tasks 1–7 cover both-succeed (Task 7), one-empty-diff prompt (Task 7 survivor), both-fail abort (Task 7), `--no-record` (Task 6 `finalize`), claims cite the commit source (Task 5), `--dry-run` no commits (Task 4 `commit=False`). ✓

**Placeholder scan:** every code step shows complete code; commands have expected output; no "TBD"/"add error handling". The only deliberately defensive step is the `CHANGELOG.md` style match (Task 8 Step 1), which adapts to the file's existing format — concrete bullet text is given.

**Type consistency:** `Candidate`/`Issue` field names are used identically across Tasks 1, 4, 5, 6, 7. `run_candidate`/`prepare`/`finalize` signatures match their call sites. `propose_claim` is called with the real keyword args verified from `proposals.py` (`text`, `evidence`, `proposed_by`, `claim_type`, `confidence`, `tags`, `rationale`). `put_source` args match `storage.py`. `SourceType.COMMIT` exists in `models.py`.
