"""PR cache + dedup ("don't raise a PR someone already tried").

Sweeps merged/closed PRs for a target GitHub repo into a local JSON file so
that a contributor (or an agent driving Claude Code) can ask "has anyone
already attempted this fix?" before opening a new PR.

Three layers, all stdlib:

* ``gh`` CLI is shelled out for the GitHub data (no pip dep, picks up the
  user's existing auth)
* JSON file under ``$XDG_CACHE_HOME/vouch/pr-cache/`` for persistence
* Optional close-reason analysis via either the local ``claude`` CLI or a
  direct Anthropic Messages API call (``urllib``) -- both gated on what the
  caller actually has available, with graceful no-op when neither is set up.

The cache schema is intentionally append-friendly: each ``build`` call upserts
by PR number, so reruns add only what's new, and existing close-reason
analyses are preserved unless ``--reanalyze`` is passed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# --- repo URL parsing -----------------------------------------------------


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def cache_key(self) -> str:
        return f"{self.owner}__{self.name}"


_REPO_PATTERNS = (
    # https://github.com/owner/repo[.git][/...]
    re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/?#].*)?$"),
    # git@github.com:owner/repo[.git]
    re.compile(r"^git@github\.com:([\w.-]+)/([\w.-]+?)(?:\.git)?$"),
    # owner/repo shorthand
    re.compile(r"^([\w.-]+)/([\w.-]+?)(?:\.git)?$"),
)


def parse_repo(ref: str) -> RepoRef:
    """Accept the URL shapes ``gh`` itself accepts plus bare ``owner/repo``.

    Raises ``ValueError`` on anything else -- the CLI layer turns that into a
    clean ``Error: ...`` line via the existing ``_cli_errors`` context.
    """
    s = ref.strip()
    for pat in _REPO_PATTERNS:
        m = pat.match(s)
        if m:
            owner, name = m.group(1), m.group(2)
            if owner and name:
                return RepoRef(owner=owner, name=name)
    raise ValueError(
        f"could not parse {ref!r} as a GitHub repo URL "
        "(expected https://github.com/<owner>/<repo>, git@github.com:<owner>/<repo>, "
        "or <owner>/<repo>)"
    )


# --- cache location -------------------------------------------------------


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/vouch/pr-cache`` (or ``~/.cache/vouch/pr-cache``).

    Overridable per-invocation via ``VOUCH_PR_CACHE_DIR`` for tests + power
    users who want a project-local cache (e.g. checked into ``.vouch/``).
    """
    override = os.environ.get("VOUCH_PR_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "vouch" / "pr-cache"


def cache_path_for(repo: RepoRef, base: Path | None = None) -> Path:
    return (base or default_cache_dir()) / f"{repo.cache_key}.json"


# --- record shape ---------------------------------------------------------


@dataclass
class CloseAnalysis:
    reason: str
    do_not_repeat: list[str] = field(default_factory=list)
    confidence: str = "low"  # low | medium | high
    analyzer: str = "none"  # claude-cli | anthropic-api | none
    analyzed_at: str = ""


@dataclass
class PRRecord:
    number: int
    state: str  # merged | closed
    title: str
    body_excerpt: str
    author: str
    files: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    issue_refs: list[int] = field(default_factory=list)  # #N references in body
    merged_at: str | None = None
    closed_at: str | None = None
    url: str = ""
    close_analysis: CloseAnalysis | None = None


# --- gh CLI thin wrapper --------------------------------------------------


class GHError(RuntimeError):
    """``gh`` is missing, unauthenticated, or returned non-zero."""


def _run_gh(args: Sequence[str], timeout: float = 60.0) -> str:
    if shutil.which("gh") is None:
        raise GHError(
            "the GitHub CLI (`gh`) is required but was not found on PATH; "
            "install from https://cli.github.com or set up an alternative."
        )
    try:
        res = subprocess.run(
            ["gh", *args],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise GHError(f"gh {' '.join(args)} timed out after {timeout:.0f}s") from e
    if res.returncode != 0:
        # Surface stderr -- gh's error messages are typically the most
        # actionable thing the caller can act on (auth, rate limit, etc.).
        raise GHError(f"gh exited {res.returncode}: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


_ISSUE_REF_RE = re.compile(r"(?<!\w)#(\d{2,7})\b")


def _extract_issue_refs(body: str) -> list[int]:
    seen: list[int] = []
    for m in _ISSUE_REF_RE.finditer(body or ""):
        n = int(m.group(1))
        if n not in seen:
            seen.append(n)
    return seen


def _gh_pr_list(repo: RepoRef, state: str, limit: int) -> list[dict[str, Any]]:
    # Cheap pass: just the metadata we need to merge into the cache.
    # File lists come from a per-PR follow-up so we can keep this list call
    # bounded (--json files on bulk list is unbounded per PR).
    fields = "number,state,title,body,author,labels,mergedAt,closedAt,url,isCrossRepository"
    out = _run_gh([
        "pr", "list",
        "--repo", repo.slug,
        "--state", state,
        "--limit", str(limit),
        "--json", fields,
    ])
    return json.loads(out or "[]")


def _gh_pr_files(repo: RepoRef, number: int) -> list[str]:
    try:
        out = _run_gh([
            "pr", "view", str(number),
            "--repo", repo.slug,
            "--json", "files",
        ])
    except GHError as e:
        log.warning("file list for %s#%d failed: %s", repo.slug, number, e)
        return []
    payload = json.loads(out or "{}")
    return [f["path"] for f in payload.get("files", []) if isinstance(f, dict) and "path" in f]


def _gh_pr_review_comments(repo: RepoRef, number: int) -> str:
    """Concatenate the review thread + close comment for close-reason analysis.

    Best-effort: we want the *human signal* about why the PR was closed, which
    typically lives in the trailing review comments. Failure here is non-fatal.
    """
    try:
        out = _run_gh([
            "pr", "view", str(number),
            "--repo", repo.slug,
            "--json", "comments,reviews",
        ])
    except GHError as e:
        log.warning("comments for %s#%d failed: %s", repo.slug, number, e)
        return ""
    payload = json.loads(out or "{}")
    parts: list[str] = []
    for c in payload.get("comments", []):
        body = (c or {}).get("body") or ""
        author = ((c or {}).get("author") or {}).get("login") or "?"
        if body.strip():
            parts.append(f"[comment by {author}]\n{body.strip()}")
    for r in payload.get("reviews", []):
        body = (r or {}).get("body") or ""
        author = ((r or {}).get("author") or {}).get("login") or "?"
        state = (r or {}).get("state") or ""
        if body.strip():
            parts.append(f"[review by {author} ({state})]\n{body.strip()}")
    return "\n\n".join(parts)


# --- close-reason analyzer (Claude Code or Anthropic API) -----------------


_ANALYZE_PROMPT = """\
You are reviewing a CLOSED-NOT-MERGED GitHub pull request.

Your job: figure out *why* it was closed and surface concrete things a future
contributor should NOT repeat. Be concrete and actionable. Do not speculate
beyond what the title/body/comments support; if the evidence is thin, say so
and mark confidence low.

PR title: {title}
PR body excerpt:
{body}

Reviewer / closer comments (most recent last):
{comments}

Reply as MINIFIED JSON with exactly these keys -- no prose, no code fence:
  {{
    "reason": "<one-sentence summary of why the PR was closed>",
    "do_not_repeat": ["<short bullet>", "..."],
    "confidence": "low" | "medium" | "high"
  }}
"""


def _truncate(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _build_analysis_prompt(pr: PRRecord, comments: str) -> str:
    return _ANALYZE_PROMPT.format(
        title=_truncate(pr.title, 200),
        body=_truncate(pr.body_excerpt, 4000),
        comments=_truncate(comments, 6000) or "(no human comments captured)",
    )


def _parse_analysis_json(raw: str) -> dict[str, Any] | None:
    # Models occasionally wrap JSON in prose despite the instruction;
    # extract the outermost {...} block tolerantly.
    raw = (raw or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    blob = raw[start : end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _now_iso() -> str:
    # No Date.now()-style hidden-state worries here; this is server-side code,
    # not a deterministic workflow script.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _analyze_via_claude_cli(prompt: str, timeout: float) -> str | None:
    """Use the local ``claude`` CLI (e.g. Claude Code) if present.

    Invokes ``claude --print`` so we get the assistant's reply on stdout with
    no interactive prompt. Returns ``None`` on absence / failure -- caller
    falls back to the API path or skips analysis.
    """
    if shutil.which("claude") is None:
        return None
    try:
        res = subprocess.run(
            ["claude", "--print"],
            input=prompt,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timed out during PR close-reason analysis")
        return None
    if res.returncode != 0:
        log.warning("claude CLI exited %d: %s", res.returncode, (res.stderr or "").strip())
        return None
    return res.stdout


def _analyze_via_anthropic_api(prompt: str, timeout: float) -> str | None:
    """Direct Messages API call via stdlib so we don't add a dep.

    Honours ``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL`` (defaults to
    ``https://api.anthropic.com``), and ``ANTHROPIC_MODEL`` (defaults to a
    cheap Sonnet/Haiku class model -- callers can override).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    payload = json.dumps({
        "model": model,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("anthropic API call failed: %s", e)
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("anthropic API returned non-JSON: %s", body[:200])
        return None
    blocks = data.get("content") or []
    out = "".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )
    return out or None


def analyze_close_reason(
    pr: PRRecord,
    comments: str,
    *,
    timeout: float = 45.0,
    prefer: str = "auto",  # auto | claude-cli | anthropic-api | none
) -> CloseAnalysis | None:
    """Returns ``None`` only if every available analyzer failed AND none was
    even configured -- callers treat that as "skip, leave existing alone"."""
    if prefer == "none":
        return None
    prompt = _build_analysis_prompt(pr, comments)
    raw: str | None = None
    analyzer = "none"
    order = (
        ["claude-cli", "anthropic-api"]
        if prefer in ("auto", "claude-cli")
        else ["anthropic-api", "claude-cli"]
        if prefer == "anthropic-api"
        else [prefer]
    )
    for which in order:
        if which == "claude-cli":
            raw = _analyze_via_claude_cli(prompt, timeout)
        elif which == "anthropic-api":
            raw = _analyze_via_anthropic_api(prompt, timeout)
        if raw:
            analyzer = which
            break
    if not raw:
        return None
    parsed = _parse_analysis_json(raw)
    if not parsed:
        log.warning("analyzer %s returned unparseable output for #%d", analyzer, pr.number)
        return CloseAnalysis(
            reason="(analyzer returned unparseable output; see PR comments)",
            do_not_repeat=[],
            confidence="low",
            analyzer=analyzer,
            analyzed_at=_now_iso(),
        )
    do_not_repeat = [
        str(x).strip()
        for x in (parsed.get("do_not_repeat") or [])
        if str(x).strip()
    ]
    return CloseAnalysis(
        reason=str(parsed.get("reason", "")).strip() or "(no reason given)",
        do_not_repeat=do_not_repeat,
        confidence=str(parsed.get("confidence", "low")).strip().lower() or "low",
        analyzer=analyzer,
        analyzed_at=_now_iso(),
    )


# --- cache I/O ------------------------------------------------------------


def _record_to_json(r: PRRecord) -> dict[str, Any]:
    d = asdict(r)
    # asdict serialises nested dataclass to dict already
    return d


def _record_from_json(d: dict[str, Any]) -> PRRecord:
    ca_d = d.get("close_analysis")
    ca = CloseAnalysis(**ca_d) if ca_d else None
    return PRRecord(
        number=int(d["number"]),
        state=str(d["state"]),
        title=str(d.get("title", "")),
        body_excerpt=str(d.get("body_excerpt", "")),
        author=str(d.get("author", "")),
        files=[str(x) for x in d.get("files", [])],
        labels=[str(x) for x in d.get("labels", [])],
        issue_refs=[int(x) for x in d.get("issue_refs", [])],
        merged_at=d.get("merged_at"),
        closed_at=d.get("closed_at"),
        url=str(d.get("url", "")),
        close_analysis=ca,
    )


def load_cache(path: Path) -> dict[str, PRRecord]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(d["number"]): _record_from_json(d) for d in raw.get("prs", [])}


def save_cache(path: Path, repo: RepoRef, records: dict[str, PRRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prs_sorted = sorted(records.values(), key=lambda r: r.number, reverse=True)
    payload = {
        "repo": repo.slug,
        "schema_version": 1,
        "fetched_at": _now_iso(),
        "count": len(prs_sorted),
        "prs": [_record_to_json(r) for r in prs_sorted],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


# --- build (fetch + merge + optionally analyze) ---------------------------


def _author_login(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("login") or raw.get("name") or "")
    return str(raw or "")


def _labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if isinstance(x, dict) and x.get("name"):
            out.append(str(x["name"]))
        elif isinstance(x, str):
            out.append(x)
    return out


def _row_to_record(row: dict[str, Any], files: list[str]) -> PRRecord:
    body = str(row.get("body") or "")
    raw_state = str(row.get("state") or "").lower()
    state = "merged" if (raw_state == "merged" or row.get("mergedAt")) else "closed"
    return PRRecord(
        number=int(row["number"]),
        state=state,
        title=str(row.get("title") or ""),
        body_excerpt=_truncate(body, 8000),
        author=_author_login(row.get("author")),
        files=files,
        labels=_labels(row.get("labels")),
        issue_refs=_extract_issue_refs(body),
        merged_at=row.get("mergedAt"),
        closed_at=row.get("closedAt"),
        url=str(row.get("url") or ""),
        close_analysis=None,
    )


@dataclass
class BuildResult:
    fetched: int
    new: int
    updated: int
    analyzed: int
    skipped_analysis: int
    path: Path


def build(
    repo: RepoRef,
    *,
    state: str = "all",  # merged | closed | all
    limit: int = 200,
    analyze_closed: bool = False,
    reanalyze: bool = False,
    analyzer: str = "auto",
    cache_dir: Path | None = None,
    fetch_files: bool = True,
) -> BuildResult:
    """Fetch + upsert. Does not delete cache entries that drop off the
    server-side list -- the historical record is the point.

    ``state``:
      * ``merged`` -- only merged PRs (cheaper; the "what's been done" signal)
      * ``closed`` -- closed-but-not-merged PRs (the "what was tried and rejected" signal)
      * ``all``    -- both
    """
    if state not in {"merged", "closed", "all"}:
        raise ValueError(f"state must be merged|closed|all, got {state!r}")
    path = cache_path_for(repo, cache_dir)
    existing = load_cache(path)

    # gh's pr list takes a single state filter; emulate "all" with two passes.
    states = ["merged", "closed"] if state == "all" else [state]
    rows: list[dict[str, Any]] = []
    for st in states:
        rows.extend(_gh_pr_list(repo, st, limit))

    new_count = 0
    updated_count = 0
    analyzed = 0
    skipped = 0

    for row in rows:
        num_key = str(row["number"])
        files = _gh_pr_files(repo, int(row["number"])) if fetch_files else []
        rec = _row_to_record(row, files)
        prev = existing.get(num_key)
        if prev is None:
            new_count += 1
        else:
            # Preserve any existing close-reason analysis unless we're told
            # to redo it -- analysis is the expensive part.
            if not reanalyze and prev.close_analysis is not None:
                rec.close_analysis = prev.close_analysis
            updated_count += 1
        if rec.state == "closed" and analyze_closed and (rec.close_analysis is None or reanalyze):
            comments = _gh_pr_review_comments(repo, rec.number)
            ca = analyze_close_reason(rec, comments, prefer=analyzer)
            if ca is None:
                skipped += 1
            else:
                rec.close_analysis = ca
                analyzed += 1
        existing[num_key] = rec

    save_cache(path, repo, existing)
    return BuildResult(
        fetched=len(rows),
        new=new_count,
        updated=updated_count,
        analyzed=analyzed,
        skipped_analysis=skipped,
        path=path,
    )


# --- dedup check ----------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "this", "that", "fix",
    "add", "use", "via", "make", "feat", "chore", "refactor", "test",
    "docs", "doc", "bug", "issue", "pr", "patch", "support", "improve",
    "update", "remove", "rename", "wip", "draft",
})


def _tokens(s: str) -> set[str]:
    return {
        t.lower()
        for t in _TOKEN_RE.findall(s or "")
        if t.lower() not in _STOPWORDS and len(t) > 2
    }


def _path_tokens(paths: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for p in paths:
        for seg in re.split(r"[/\\.]+", p or ""):
            if seg and seg.lower() not in _STOPWORDS and len(seg) > 2:
                out.add(seg.lower())
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _containment(a: set[str], b: set[str]) -> float:
    """Overlap coefficient -- ``|A ∩ B| / min(|A|, |B|)``.

    Asymmetric in spirit but symmetric in form: robust to one side having
    much more text than the other. The right metric for "is my new PR
    essentially a redo of this cached one?" because we don't want to be
    penalised by the cached PR having a long body.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class DupCandidate:
    pr: PRRecord
    score: float
    title_overlap: float
    path_overlap: float

    def as_json(self) -> dict[str, Any]:
        return {
            "number": self.pr.number,
            "state": self.pr.state,
            "title": self.pr.title,
            "url": self.pr.url,
            "score": round(self.score, 3),
            "title_overlap": round(self.title_overlap, 3),
            "path_overlap": round(self.path_overlap, 3),
            "files": self.pr.files,
            "close_analysis": asdict(self.pr.close_analysis) if self.pr.close_analysis else None,
        }


def check_duplicates(
    cache: dict[str, PRRecord],
    *,
    topic: str,
    files: Sequence[str] | None = None,
    min_score: float = 0.15,
    top_k: int = 5,
) -> list[DupCandidate]:
    """Rank cached PRs by lexical + path overlap with ``topic``+``files``.

    Score is a weighted mix: 0.7 * title-token *overlap coefficient* + 0.3 *
    file-path Jaccard (only when ``files`` is provided -- otherwise
    title-only). The overlap coefficient (``|A∩B| / min(|A|,|B|)``) is the
    right metric for "is my new PR essentially a redo of this one?" because
    it ignores the cached PR's long body -- a topic whose words all appear
    in a cached PR's title scores ~1.0 even if the cached PR also discusses
    twenty other things.

    Closed-not-merged matches outrank merged ones at equal score: a
    near-miss against a previously *rejected* PR is far more valuable to
    surface than overlap with a long-merged one, because "this approach was
    tried and rejected" is the signal that prevents wasted work.
    """
    topic_tokens = _tokens(topic)
    file_tokens = _path_tokens(files or [])
    out: list[DupCandidate] = []
    for rec in cache.values():
        rec_tokens = _tokens(rec.title + " " + rec.body_excerpt)
        t_overlap = _containment(topic_tokens, rec_tokens)
        p_overlap = _jaccard(file_tokens, _path_tokens(rec.files)) if file_tokens else 0.0
        score = (0.7 * t_overlap + 0.3 * p_overlap) if file_tokens else t_overlap
        if score >= min_score:
            out.append(DupCandidate(
                pr=rec, score=score,
                title_overlap=t_overlap, path_overlap=p_overlap,
            ))
    # Sort by score desc, then closed-before-merged as the tiebreak so a
    # rejected attempt at the same idea surfaces above a merged one.
    out.sort(key=lambda c: (c.score, c.pr.state == "closed"), reverse=True)
    return out[:top_k]
