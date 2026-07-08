# Friendlier MCP Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make vouch feel as friendly as pmb by narrowing the MCP tool surface an agent sees (58 → ~8 by default), fusing retrieval by default, and injecting relevant context on every prompt — all without touching the review gate.

**Architecture:** A new `mcp_profiles` module filters which `@mcp.tool()` tools the stdio server exposes (applied in `run_stdio`, not at import, so tests and the parity check still see all 58). `context._retrieve` is rewritten so `auto`/`hybrid` fuse embedding + FTS5 via the existing `rrf_fuse` instead of a first-non-empty waterfall, with a cheap near-duplicate drop. A tiny, always-safe `hooks` helper + a hidden `vouch context-hook` CLI command feed a Claude Code `UserPromptSubmit` hook so recall costs zero tool calls.

**Tech Stack:** Python 3.11–3.13, `mcp.server.fastmcp.FastMCP`, click CLI, pytest, pydantic v2, yaml config, sqlite FTS5 + optional embeddings.

## Global Constraints

- **Review gate is untouched.** No task changes the write path (`proposals.py`, `lifecycle.py`, `storage.put_*`). Profiles change *exposure* only.
- **Protocol surface unchanged.** `capabilities.METHODS`, the JSONL `HANDLERS`, and the CLI keep all 58 methods. Only which MCP tools are *shown* narrows.
- **Default profile is `minimal`.** Resolution: `VOUCH_TOOL_PROFILE` env var > `config.yaml` `mcp.tool_profile` > `"minimal"`. Unknown → `"minimal"`.
- **CI gate must stay green:** `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings`, `.venv/bin/python -m mypy src`, `.venv/bin/python -m ruff check src tests`. Every new function is fully type-annotated (mypy `src` is strict).
- **Commits:** conventional-commit format, summary ≤72 chars, lowercase body, **no `Co-Authored-By` trailer**. This repo's hooks reject `git commit -m` with heredocs and may reject `-m` outright — write the message to `/tmp/msg.txt` with the Write tool (never a heredoc) and use `git commit -F /tmp/msg.txt`. Stage files by name, never `git add -A`.
- **Branch:** `feat/friendlier-mcp-surface` (already created off `origin/main`; the spec is already committed on it).
- **Profile tool sets (dotted `METHODS` names), authoritative:**
  - `minimal` (8): `kb.capabilities`, `kb.status`, `kb.context`, `kb.search`, `kb.read_page`, `kb.propose_claim`, `kb.propose_page`, `kb.list_pending`
  - `standard` (minimal + 9 = 17): + `kb.approve`, `kb.reject`, `kb.supersede`, `kb.contradict`, `kb.confirm`, `kb.read_claim`, `kb.list_claims`, `kb.neighbors`, `kb.why`
  - `full`: all of `capabilities.METHODS` (58)
- MCP tool names use `_`; `METHODS` uses `.`. Transform: `"kb.foo_bar"` ↔ `"kb_foo_bar"` via the substring after the first separator.

---

### Task 1: MCP tool profiles

**Files:**
- Create: `src/vouch/mcp_profiles.py`
- Modify: `src/vouch/server.py` (add import; apply profile inside `run_stdio` at `server.py:1011-1015`)
- Test: `tests/test_mcp_profiles.py`

**Interfaces:**
- Produces: `mcp_profiles.PROFILES: dict[str, frozenset[str]]`, `mcp_profiles.DEFAULT_PROFILE: str`, `resolve_profile_name(config: dict | None = None) -> str`, `tool_names_for(name: str) -> set[str]`, `apply_tool_profile(mcp, name: str) -> list[str]` (returns removed underscore tool names, sorted).
- Consumes (Task 6): `compact_descriptions(mcp) -> int` is added to this module later.

- [ ] **Step 1: Write the failing test** — create `tests/test_mcp_profiles.py`:

```python
"""MCP tool profiles narrow the surface an agent sees (friendlier-mcp slice)."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp import FastMCP

from vouch import mcp_profiles
from vouch.capabilities import METHODS

_MINIMAL_TOOLS = {
    "kb_capabilities", "kb_status", "kb_context", "kb_search",
    "kb_read_page", "kb_propose_claim", "kb_propose_page", "kb_list_pending",
}


def _make(name: str):
    def fn(x: int = 0) -> int:
        return x
    fn.__name__ = name
    return fn


def _fresh_mcp() -> FastMCP:
    m = FastMCP("probe")
    for method in METHODS:
        m.tool()(_make("kb_" + method.split(".", 1)[1]))
    return m


def test_full_profile_removes_nothing() -> None:
    m = _fresh_mcp()
    removed = mcp_profiles.apply_tool_profile(m, "full")
    assert removed == []
    assert len(m._tool_manager._tools) == len(METHODS)


def test_minimal_exposes_core_only() -> None:
    m = _fresh_mcp()
    mcp_profiles.apply_tool_profile(m, "minimal")
    assert set(m._tool_manager._tools) == _MINIMAL_TOOLS


def test_standard_is_superset_of_minimal() -> None:
    assert mcp_profiles.PROFILES["minimal"] <= mcp_profiles.PROFILES["standard"]


def test_every_profile_is_subset_of_methods() -> None:
    allm = set(METHODS)
    for name, methods in mcp_profiles.PROFILES.items():
        assert methods <= allm, f"{name} references non-methods: {methods - allm}"
    assert mcp_profiles.PROFILES["full"] == allm


def test_resolve_env_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_TOOL_PROFILE", "full")
    assert mcp_profiles.resolve_profile_name({"mcp": {"tool_profile": "minimal"}}) == "full"


def test_resolve_default_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOUCH_TOOL_PROFILE", raising=False)
    assert mcp_profiles.resolve_profile_name(None) == "minimal"


def test_unknown_profile_falls_back_to_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOUCH_TOOL_PROFILE", "bogus")
    assert mcp_profiles.resolve_profile_name(None) == "minimal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_profiles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.mcp_profiles'`.

- [ ] **Step 3: Write minimal implementation** — create `src/vouch/mcp_profiles.py`:

```python
"""MCP tool profiles — narrow the tool surface an agent sees by default.

vouch exposes 58 kb.* methods. Handing all of them to an agent every turn is
the main first-touch friendliness cost (the closest competitor, pmb, exposes
~10 by default and hides the rest behind a profile flag). Profiles control
*exposure* only: the JSONL and CLI surfaces, the protocol method list
(capabilities.METHODS), and the review gate are unchanged.

Resolution order: VOUCH_TOOL_PROFILE env var > config.yaml `mcp.tool_profile`
> "minimal" (the default). Unknown names fall back to "minimal".
"""

from __future__ import annotations

import os
from typing import Any

from .capabilities import METHODS

_MINIMAL: frozenset[str] = frozenset({
    "kb.capabilities",
    "kb.status",
    "kb.context",
    "kb.search",
    "kb.read_page",
    "kb.propose_claim",
    "kb.propose_page",
    "kb.list_pending",
})

_STANDARD: frozenset[str] = _MINIMAL | frozenset({
    "kb.approve",
    "kb.reject",
    "kb.supersede",
    "kb.contradict",
    "kb.confirm",
    "kb.read_claim",
    "kb.list_claims",
    "kb.neighbors",
    "kb.why",
})

PROFILES: dict[str, frozenset[str]] = {
    "minimal": _MINIMAL,
    "standard": _STANDARD,
    "full": frozenset(METHODS),
}

DEFAULT_PROFILE = "minimal"


def _tool_name(method: str) -> str:
    """`"kb.propose_claim"` -> `"kb_propose_claim"` (MCP tool names use `_`)."""
    return "kb_" + method.split(".", 1)[1]


def resolve_profile_name(config: dict[str, Any] | None = None) -> str:
    """Pick the active profile from env > config > default."""
    raw = os.environ.get("VOUCH_TOOL_PROFILE")
    if not raw and config:
        raw = config.get("mcp", {}).get("tool_profile")
    name = str(raw).strip().lower() if raw else DEFAULT_PROFILE
    return name if name in PROFILES else DEFAULT_PROFILE


def tool_names_for(name: str) -> set[str]:
    """The MCP (underscore) tool names exposed by profile `name`."""
    return {_tool_name(m) for m in PROFILES.get(name, PROFILES[DEFAULT_PROFILE])}


def apply_tool_profile(mcp: Any, name: str) -> list[str]:
    """Remove every registered `kb_*` MCP tool not in profile `name`.

    Returns the sorted list of removed tool names. Idempotent. `full` removes
    nothing. Only `kb_*` tools are touched, so trust/diagnostic tools
    registered elsewhere are never dropped.
    """
    keep = tool_names_for(name)
    tools = mcp._tool_manager._tools
    removed: list[str] = []
    for tool_name in list(tools.keys()):
        if tool_name.startswith("kb_") and tool_name not in keep:
            tools.pop(tool_name, None)
            removed.append(tool_name)
    return sorted(removed)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mcp_profiles.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Wire the profile into the stdio server** — modify `src/vouch/server.py`. Add to the imports near the top (with the other `from .` imports, e.g. after `from .synthesize import synthesize` at line 56):

```python
from . import mcp_profiles
```

Then replace `run_stdio` (currently `server.py:1011-1015`):

```python
def run_stdio() -> None:
    """Entry point used by `vouch serve`."""
    configure_logging()
    trust_mod.set_stdio_default(trust_mod.MCP_STDIO)
    try:
        cfg: dict[str, Any] | None = _load_cfg(_store())
    except Exception:
        cfg = None
    mcp_profiles.apply_tool_profile(mcp, mcp_profiles.resolve_profile_name(cfg))
    mcp.run()
```

(`Any` is already imported in `server.py`; `_load_cfg` is at `server.py:204` and `_store` at `server.py:61`.)

- [ ] **Step 6: Verify server still imports and mypy/ruff pass**

Run: `.venv/bin/python -c "import vouch.server"` → Expected: no output, exit 0.
Run: `.venv/bin/python -m mypy src/vouch/mcp_profiles.py src/vouch/server.py` → Expected: `Success`.
Run: `.venv/bin/python -m ruff check src/vouch/mcp_profiles.py src/vouch/server.py` → Expected: `All checks passed!`.

- [ ] **Step 7: Commit**

Write `/tmp/msg.txt`:
```
feat(mcp): add tool profiles, expose a minimal surface by default

agents saw all 58 kb.* tools every turn — the main first-touch friendliness
gap vs pmb, which exposes ~10 by default. add a profile layer applied in
run_stdio: minimal (8 core tools) by default, standard (16), or full (58),
selected by VOUCH_TOOL_PROFILE / config mcp.tool_profile. exposure only — the
protocol surface, jsonl/cli, and the review gate are unchanged.
```
Run:
```bash
git add src/vouch/mcp_profiles.py src/vouch/server.py tests/test_mcp_profiles.py
git commit -F /tmp/msg.txt
```

---

### Task 2: Enforce true MCP↔METHODS parity

**Files:**
- Modify: `tests/test_capabilities.py`

**Interfaces:**
- Consumes: `vouch.server.mcp` (the module-level, unfiltered `FastMCP`), `vouch.capabilities.METHODS`.

**Why:** `test_capabilities.py` today only checks `capabilities.METHODS == JSONL HANDLERS`. The 58 MCP tools were never compared to `METHODS`, so MCP drift passed CI (the CLAUDE.md "all three surfaces" claim was really 2-of-3). Importing `vouch.server` registers all tools but does **not** apply a profile (that happens only in `run_stdio`), so this sees the full surface.

- [ ] **Step 1: Write the failing test** — append to `tests/test_capabilities.py`:

```python
def test_mcp_tools_match_methods() -> None:
    """Every MCP kb_* tool maps to a capabilities method and vice-versa.

    Closes the MCP half of the 3-surface parity invariant that the JSONL
    check above did not cover. Uses the unfiltered server object (profiles
    apply only in run_stdio).
    """
    from vouch.server import mcp

    tool_names = {n for n in mcp._tool_manager._tools if n.startswith("kb_")}
    as_methods = {"kb." + n.split("_", 1)[1] for n in tool_names}
    declared = set(capabilities.METHODS)
    assert as_methods == declared, (
        f"mcp/methods mismatch: "
        f"missing tools={declared - as_methods}, "
        f"undeclared tools={as_methods - declared}"
    )
```

- [ ] **Step 2: Run test to verify it passes or reveals real drift**

Run: `.venv/bin/python -m pytest tests/test_capabilities.py -q`
Expected: PASS if the 58 tools already align with `METHODS`. If it FAILS, the message names the exact drift — fix `capabilities.METHODS` or the tool registration in `server.py` until it passes (this is a real bug the test just caught, not a test error). Do not weaken the assertion.

- [ ] **Step 3: Commit**

Write `/tmp/msg.txt`:
```
test(capabilities): assert mcp tool set matches the method list

the parity test only compared capabilities.METHODS to the jsonl handlers;
the 58 mcp tools were never checked, so mcp drift passed ci. enumerate the
unfiltered server tools and assert they equal METHODS — the real 3-surface
check for the mcp side.
```
Run:
```bash
git add tests/test_capabilities.py
git commit -F /tmp/msg.txt
```

---

### Task 3: Fuse retrieval by default

**Files:**
- Modify: `src/vouch/context.py` (`_VALID_BACKENDS` at line 45; rewrite `_retrieve` body at lines 90-120; add a top-level import)
- Modify: `src/vouch/storage.py` (default backend at line 96)
- Modify: `tests/test_retrieval_backend.py` (update the two `auto` tests whose behavior intentionally changes; add a hybrid-fusion test)

**Interfaces:**
- Consumes: `vouch.embeddings.fusion.rrf_fuse(a, b, *, limit=10, k=60)` where `a`/`b` are `list[tuple[str, str, str, float]]` (kind, id, summary, score).
- Produces: `_retrieve` now tags fused hits with backend `"hybrid"`; `auto` and `hybrid` both fuse embedding + FTS5.

- [ ] **Step 1: Update the two tests whose behavior changes + add the fusion test.** In `tests/test_retrieval_backend.py`, replace `test_backend_auto_prefers_embedding` (lines 82-89) and `test_unset_backend_defaults_to_auto` (lines 92-101) with:

```python
def test_backend_auto_now_fuses(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto` no longer waterfalls embedding-first; it fuses embedding + fts5
    (RRF) and tags hits `hybrid`."""
    _force_semantic_hit(monkeypatch)
    _set_backend(store, "auto")
    pack = context.build_context_pack(store, query="JWT")
    assert pack["items"]
    assert _backends(pack) == {"hybrid"}


def test_unset_backend_fuses(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config with no retrieval.backend behaves like fused `auto`."""
    _force_semantic_hit(monkeypatch)
    cfg = yaml.safe_load(store.config_path.read_text())
    cfg.get("retrieval", {}).pop("backend", None)
    store.config_path.write_text(yaml.safe_dump(cfg))
    pack = context.build_context_pack(store, query="JWT")
    assert _backends(pack) == {"hybrid"}


def test_backend_hybrid_merges_semantic_and_lexical(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hybrid` returns the union of both retrievers, not first-non-empty."""
    src = store.put_source(b"e2")
    store.put_claim(Claim(id="c2", text="OAuth refresh flow", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "JWT token rotation", 0.99)],
    )
    monkeypatch.setattr(
        context.index_db, "search",
        lambda *a, **k: [("claim", "c2", "OAuth refresh flow", 0.88)],
    )
    _set_backend(store, "hybrid")
    pack = context.build_context_pack(store, query="auth")
    assert {item["id"] for item in pack["items"]} == {"c1", "c2"}
    assert _backends(pack) == {"hybrid"}
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/python -m pytest tests/test_retrieval_backend.py -q`
Expected: FAIL — `test_backend_auto_now_fuses` / `test_unset_backend_fuses` / `test_backend_hybrid_merges_semantic_and_lexical` fail because `hybrid` isn't accepted yet and `auto` still waterfalls (backend is `embedding`, not `hybrid`).

- [ ] **Step 3: Implement fusion.** In `src/vouch/context.py`:

(a) Add near the top imports (after the existing `from . import` block):
```python
from .embeddings.fusion import rrf_fuse
```
(`fusion.py` is pure-Python with no heavy deps, so a top-level import is safe under the base install.)

(b) Change `_VALID_BACKENDS` (line 45):
```python
_VALID_BACKENDS = ("auto", "hybrid", "embedding", "fts5", "substring")
```

(c) Replace the body of `_retrieve` from line 90 (`backend = _configured_backend(store)`) through line 120 (the final substring `return`) with:
```python
    backend = _configured_backend(store)
    fetch_limit = scoped_fetch_limit(limit, viewer)

    if backend in ("auto", "hybrid"):
        sem = index_db.search_semantic(store.kb_dir, query, limit=fetch_limit)
        try:
            lex = index_db.search(store.kb_dir, query, limit=fetch_limit)
        except sqlite3.Error:
            lex = []
        fused = rrf_fuse(sem, lex, limit=fetch_limit)
        if fused:
            filtered = filter_hits(store, fused, viewer, limit=limit)
            return [(k, i, s, sc, "hybrid") for k, i, s, sc in filtered]
        # both retrievers empty -> fall through to the substring scan below.

    if backend == "embedding":
        raw = index_db.search_semantic(store.kb_dir, query, limit=fetch_limit)
        if raw:
            filtered = filter_hits(store, raw, viewer, limit=limit)
            return [(k, i, s, sc, "embedding") for k, i, s, sc in filtered]
        return []

    if backend == "fts5":
        try:
            hits = index_db.search(store.kb_dir, query, limit=fetch_limit)
            if hits:
                filtered = filter_hits(store, hits, viewer, limit=limit)
                return [(k, i, s, sc, "fts5") for k, i, s, sc in filtered]
        except sqlite3.Error:
            pass
        return []

    substring_hits = store.search_substring(query, limit=fetch_limit)
    filtered = filter_hits(store, substring_hits, viewer, limit=limit)
    return [(k, i, s, sc, "substring") for k, i, s, sc in filtered]
```

(d) In `src/vouch/storage.py` line 96, change the init default so new KBs say `hybrid` explicitly:
```python
            "backend": "hybrid",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_retrieval_backend.py -q`
Expected: PASS. `test_backend_fts5_skips_embedding`, `test_backend_embedding_is_recognized`, `test_backend_substring_only` still pass (explicit pins unchanged).

- [ ] **Step 5: Guard against retrieval-quality regression + mypy/ruff**

Run: `.venv/bin/python -m pytest tests/test_context.py tests/test_index.py -q` → Expected: PASS.
Run: `.venv/bin/python -m vouch eval recall eval/queries.jsonl --kb eval/fixture-kb --baseline eval/baseline.json --max-regression 0.05` (the exact args `eval.yml` uses; if the CLI flags differ, run `.venv/bin/vouch eval recall --help` and match them) → Expected: no regression beyond 0.05.
Run: `.venv/bin/python -m mypy src/vouch/context.py src/vouch/storage.py` → Expected: `Success`.
Run: `.venv/bin/python -m ruff check src/vouch/context.py src/vouch/storage.py` → Expected: `All checks passed!`.

- [ ] **Step 6: Commit**

Write `/tmp/msg.txt`:
```
feat(retrieval): fuse embedding + fts5 by default instead of a waterfall

_retrieve tried embedding, then fts5, then substring, returning the first
non-empty list — so lexical and semantic hits never combined. auto and
hybrid now fuse both retrievers with the already-built rrf_fuse and tag hits
"hybrid"; explicit embedding/fts5/substring pins are unchanged. existing KBs
(config says "auto") benefit with no migration.
```
Run:
```bash
git add src/vouch/context.py src/vouch/storage.py tests/test_retrieval_backend.py
git commit -F /tmp/msg.txt
```

---

### Task 4: Drop near-duplicate context items

**Files:**
- Modify: `src/vouch/context.py` (add `_jaccard` + `_dedupe_near_duplicates`; call it in `build_context_pack` after graph expansion, ~line 256)
- Test: `tests/test_retrieval_backend.py` (add one test; reuses the existing `store` fixture)

**Interfaces:**
- Produces: `build_context_pack` drops lower-scored items whose summary is near-identical (token-set Jaccard ≥ 0.85) to a higher-scored kept item.

- [ ] **Step 1: Write the failing test** — append to `tests/test_retrieval_backend.py`:

```python
def test_near_duplicate_summaries_are_dropped(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent should not see the same fact twice."""
    src = store.put_source(b"z")
    store.put_claim(Claim(
        id="d1", text="the cache uses redis with a 60 second ttl", evidence=[src.id]))
    store.put_claim(Claim(
        id="d2", text="the cache uses redis with a 60 second ttl now", evidence=[src.id]))
    health.rebuild_index(store)
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [
            ("claim", "d1", "the cache uses redis with a 60 second ttl", 0.90),
            ("claim", "d2", "the cache uses redis with a 60 second ttl now", 0.89),
        ],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    _set_backend(store, "hybrid")
    pack = context.build_context_pack(store, query="cache")
    assert {item["id"] for item in pack["items"]} == {"d1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_retrieval_backend.py::test_near_duplicate_summaries_are_dropped -q`
Expected: FAIL — both `d1` and `d2` present.

- [ ] **Step 3: Implement the dedupe pass.** In `src/vouch/context.py`, add these helpers above `build_context_pack` (e.g. after `_append_graph_neighbors`, ~line 198):

```python
def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _dedupe_near_duplicates(items: list[ContextItem]) -> list[ContextItem]:
    """Drop later items whose summary is near-identical to a kept one.

    Cheap greedy pass (token-set Jaccard >= 0.85 over the first 40 tokens).
    `items` must arrive in descending-score order so the higher-scored
    duplicate is the one kept.
    """
    kept: list[ContextItem] = []
    kept_tokens: list[set[str]] = []
    for it in items:
        toks = set(it.summary.lower().split()[:40])
        if any(_jaccard(toks, seen) >= 0.85 for seen in kept_tokens):
            continue
        kept.append(it)
        kept_tokens.append(toks)
    return kept
```

Then in `build_context_pack`, immediately after the `if expand_graph:` block closes (after line 256, before the `failed: list[str] = []` line at 257) insert:

```python
    items = _dedupe_near_duplicates(items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_retrieval_backend.py -q` → Expected: PASS (all, including the earlier fusion tests — single non-duplicate hits are unaffected).

- [ ] **Step 5: mypy/ruff**

Run: `.venv/bin/python -m mypy src/vouch/context.py` → Expected: `Success`.
Run: `.venv/bin/python -m ruff check src/vouch/context.py` → Expected: `All checks passed!`.

- [ ] **Step 6: Commit**

Write `/tmp/msg.txt`:
```
feat(retrieval): drop near-duplicate items from the context pack

a fused pack could surface the same fact from two claims. add a cheap greedy
jaccard pass (>=0.85 over the first 40 tokens) that keeps the highest-scored
of a near-duplicate cluster, so an agent never reads the same thing twice.
```
Run:
```bash
git add src/vouch/context.py tests/test_retrieval_backend.py
git commit -F /tmp/msg.txt
```

---

### Task 5: Per-prompt auto-recall hook

**Files:**
- Create: `src/vouch/hooks.py`
- Modify: `src/vouch/cli.py` (add a hidden `context-hook` command near the `context` command at `cli.py:2272`)
- Modify: `adapters/claude-code/.claude/settings.json` (add a `UserPromptSubmit` hook)
- Test: `tests/test_hooks.py`

**Interfaces:**
- Produces: `hooks.build_claude_prompt_hook(store: KBStore, stdin_text: str) -> str` — returns the JSON envelope to inject, or `""` for nothing. Never raises.
- Consumes: `context.build_context_pack`, `cli._load_store`.

- [ ] **Step 1: Write the failing test** — create `tests/test_hooks.py`:

```python
"""The per-prompt hook injects relevant KB context with zero tool calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import context, health, hooks
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="deploys run on tuesdays via ci", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def _force_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context.index_db, "search_semantic",
        lambda *a, **k: [("claim", "c1", "deploys run on tuesdays via ci", 0.9)],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])


def test_empty_prompt_injects_nothing(store: KBStore) -> None:
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": ""})) == ""
    assert hooks.build_claude_prompt_hook(store, "") == ""


def test_relevant_prompt_yields_additional_context(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    env = json.loads(out)
    assert env["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "tuesdays" in env["hookSpecificOutput"]["additionalContext"]


def test_raw_non_json_stdin_is_tolerated(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, "when do deploys run")
    assert "tuesdays" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_no_hits_injects_nothing(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "zzznomatch"})) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vouch.hooks'`.

- [ ] **Step 3: Implement the helper** — create `src/vouch/hooks.py`:

```python
"""Host-hook helpers: translate an agent host's prompt hook into KB context.

Claude Code's UserPromptSubmit hook passes a JSON payload on stdin and injects
whatever the hook prints (as an `additionalContext` envelope) before the model
runs. `build_claude_prompt_hook` turns that payload into a compact, relevant
context block drawn from *approved* KB knowledge — so recall costs the agent
zero tool calls. It never raises: on any problem it returns "" (inject
nothing), so a hook failure can never block the user's turn.
"""

from __future__ import annotations

import json
from typing import Any

from .context import build_context_pack
from .storage import KBStore

_MAX_ITEMS = 8
_MAX_CHARS = 2000


def _render(pack: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in pack.get("items", []):
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        cites = item.get("citations") or []
        suffix = f"  [{', '.join(cites)}]" if cites else ""
        lines.append(f"- {summary}{suffix}")
    return "\n".join(lines)


def build_claude_prompt_hook(store: KBStore, stdin_text: str) -> str:
    """Return the stdout a host should inject for this prompt, or "" for none."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {"prompt": stdin_text}
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        return ""
    try:
        pack = build_context_pack(
            store, query=prompt, limit=_MAX_ITEMS, max_chars=_MAX_CHARS,
        )
    except Exception:
        return ""
    body = _render(pack) if isinstance(pack, dict) else ""
    if not body:
        return ""
    block = (
        "Relevant knowledge from the project's vouch KB "
        "(approved & cited — consider it before answering):\n" + body
    )
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_hooks.py -q` → Expected: PASS (4 tests).

- [ ] **Step 5: Add the hidden CLI command.** In `src/vouch/cli.py`, immediately after the `context` command (after `_emit_json(pack)` at `cli.py:2299`, before the `synthesize` command at 2302) add:

```python
@cli.command(name="context-hook", hidden=True)
def context_hook() -> None:
    """Emit relevant KB context for a host UserPromptSubmit hook (reads stdin).

    Wired by the claude-code adapter; not meant to be run by hand. Reads the
    host's JSON hook payload on stdin, prints an additionalContext envelope,
    and always exits 0 so it can never block a turn.
    """
    import sys

    from . import hooks

    stdin_text = sys.stdin.read()
    try:
        out = hooks.build_claude_prompt_hook(_load_store(), stdin_text)
    except Exception:
        out = ""
    if out:
        click.echo(out)
```

- [ ] **Step 6: Verify the command runs end-to-end**

Run: `printf '{"prompt":"anything"}' | .venv/bin/vouch context-hook` from inside a KB dir (or `cd eval/fixture-kb && printf '{"prompt":"jwt"}' | ../../.venv/bin/vouch context-hook`).
Expected: either empty output (no hits) or a single line of JSON containing `"hookEventName": "UserPromptSubmit"`. Never a traceback, always exit 0 (`echo $?` → `0`).

- [ ] **Step 7: Wire the hook into the claude-code adapter.** In `adapters/claude-code/.claude/settings.json`, add a `UserPromptSubmit` block inside `"hooks"` (after the `SessionStart` block, before `PostToolUse`). The file's `"hooks"` object becomes:

```json
  "hooks": {
    "SessionStart": [
      {
        "comment": "finalize old buffers from previous sessions; current session will be finalized here too on next session start (fallback: windowclose event not yet supported by claude-code extension)",
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "vouch capture finalize-all || true" },
          { "type": "command", "command": "vouch status --json || true" },
          { "type": "command", "command": "vouch capture banner || true" },
          { "type": "command", "command": "vouch recall || true" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "comment": "inject KB context relevant to THIS prompt (per-prompt auto-recall, 0 tool calls); never blocks the turn",
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "vouch context-hook || true" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "vouch capture observe || true" }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "vouch capture finalize || true" }
        ]
      }
    ]
  }
```

(`kb_context` is already in the adapter's permission allowlist, and the hook uses the CLI, so no permission change is needed. `vouch recall` stays at SessionStart — it serves the session banner, a different purpose from per-prompt recall.)

- [ ] **Step 8: Verify the adapter JSON stays valid + install-merge test passes**

Run: `.venv/bin/python -c "import json; json.load(open('adapters/claude-code/.claude/settings.json'))"` → Expected: no output, exit 0.
Run: `.venv/bin/python -m pytest tests/test_install_adapter.py -q` → Expected: PASS.

- [ ] **Step 9: Commit**

Write `/tmp/msg.txt`:
```
feat(hooks): inject per-prompt kb context via a userpromptsubmit hook

recall used to be either a session-start firehose or an explicit tool call.
add a pure, never-raising helper + a hidden `vouch context-hook` command that
reads the host's prompt payload on stdin and prints an additionalContext
envelope, and wire it into the claude-code adapter's UserPromptSubmit hook —
so relevant, approved knowledge is injected every turn with zero tool calls.
```
Run:
```bash
git add src/vouch/hooks.py src/vouch/cli.py adapters/claude-code/.claude/settings.json tests/test_hooks.py
git commit -F /tmp/msg.txt
```

---

### Task 6: Compact tool descriptions under non-full profiles

**Files:**
- Modify: `src/vouch/mcp_profiles.py` (add `compact_descriptions`)
- Modify: `src/vouch/server.py` (call it in `run_stdio` when profile != `full`)
- Test: `tests/test_mcp_profiles.py` (add one test)

**Interfaces:**
- Produces: `mcp_profiles.compact_descriptions(mcp) -> int` — trims each `kb_*` tool's description to its first line; returns count changed.

- [ ] **Step 1: Write the failing test** — append to `tests/test_mcp_profiles.py`:

```python
def test_compact_descriptions_trims_to_first_line() -> None:
    m = FastMCP("probe")

    def kb_thing(x: int = 0) -> int:
        """First line.

        Second paragraph with lots of detail the agent does not need.
        """
        return x

    m.tool()(kb_thing)
    changed = mcp_profiles.compact_descriptions(m)
    assert changed == 1
    assert m._tool_manager._tools["kb_thing"].description == "First line."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_profiles.py::test_compact_descriptions_trims_to_first_line -q`
Expected: FAIL — `AttributeError: module 'vouch.mcp_profiles' has no attribute 'compact_descriptions'`.

- [ ] **Step 3: Implement.** Append to `src/vouch/mcp_profiles.py`:

```python
def compact_descriptions(mcp: Any) -> int:
    """Trim each kb_ tool's description to its first line to save context.

    Full docstrings are only needed under the `full` profile; the first line
    is enough for an agent choosing a tool. Returns the number changed.
    """
    changed = 0
    for tool in mcp._tool_manager._tools.values():
        if not tool.name.startswith("kb_"):
            continue
        desc = tool.description or ""
        first = desc.strip().split("\n", 1)[0].strip()
        if first and first != desc:
            try:
                tool.description = first
            except (AttributeError, TypeError):
                # pydantic frozen-model fallback
                object.__setattr__(tool, "description", first)
            changed += 1
    return changed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mcp_profiles.py -q` → Expected: PASS (8 tests).

- [ ] **Step 5: Call it from the server.** In `src/vouch/server.py` `run_stdio`, insert after the `apply_tool_profile(...)` line and before `mcp.run()`:

```python
    if mcp_profiles.resolve_profile_name(cfg) != "full":
        mcp_profiles.compact_descriptions(mcp)
```

(Resolve is cheap and pure; calling it twice is fine. Alternatively hoist the resolved name into a local `profile` var and reuse — either is acceptable.)

- [ ] **Step 6: mypy/ruff + import check**

Run: `.venv/bin/python -c "import vouch.server"` → Expected: exit 0.
Run: `.venv/bin/python -m mypy src/vouch/mcp_profiles.py src/vouch/server.py` → Expected: `Success`.
Run: `.venv/bin/python -m ruff check src/vouch/mcp_profiles.py src/vouch/server.py` → Expected: `All checks passed!`.

- [ ] **Step 7: Commit**

Write `/tmp/msg.txt`:
```
feat(mcp): serve one-line tool descriptions under non-full profiles

full docstrings for every exposed tool are paid on every turn. under minimal
and standard, trim each tool's description to its first line (full keeps the
complete docstrings), cutting the per-turn context cost.
```
Run:
```bash
git add src/vouch/mcp_profiles.py src/vouch/server.py tests/test_mcp_profiles.py
git commit -F /tmp/msg.txt
```

---

## Final verification

Run the full CI gate exactly as `.github/workflows/ci.yml` does, plus a manual end-to-end (per the "verify the shipped diff" rule — exercise the real behavior, not just unit tests):

- [ ] `.venv/bin/python -m pytest tests/ -q --ignore=tests/embeddings` → all pass.
- [ ] `.venv/bin/python -m mypy src` → `Success`.
- [ ] `.venv/bin/python -m ruff check src tests` → `All checks passed!`.
- [ ] **Surface, minimal (default):** in a KB dir, run the server and count tools —
  `.venv/bin/python -c "import vouch.server as s, vouch.mcp_profiles as p; p.apply_tool_profile(s.mcp, 'minimal'); print(sorted(n for n in s.mcp._tool_manager._tools if n.startswith('kb_')))"` → exactly the 8 minimal tools.
- [ ] **Surface, full override:** same one-liner with `'full'` → all 58 present.
- [ ] **Auto-recall e2e:** `cd eval/fixture-kb && printf '{"prompt":"how is rate limiting done"}' | ../../.venv/bin/vouch context-hook` → one JSON line with `additionalContext` drawn from the fixture claims; `echo $?` → `0`.
- [ ] **Docs (light):** add one line documenting `VOUCH_TOOL_PROFILE` / `mcp.tool_profile` (default `minimal`, values `minimal|standard|full`) to `mintlify/reference/` (the config/MCP reference) and the claude-code guide. Commit as `docs(mcp): document tool profiles`.

Then hand back to the user for review before any push (this branch is off `main`; pushing is a separate, explicit step).
