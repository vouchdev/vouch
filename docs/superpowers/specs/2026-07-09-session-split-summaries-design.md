# split a huge session into separate topical pages, host-neutrally

- status: draft, awaiting review
- date: 2026-07-09
- scope: one implementation plan

## goal

when a session is large enough that a single rolled-up summary would be an
unreadable wall, summarize it *with an llm* into several coherent topical
pages instead of one — one page per thread of work. this must work the same
for any host that feeds vouch (claude code, openclaw, codex, cursor, cline,
continue, windsurf, zed, …), not just claude code. small sessions keep today's
mechanical single-page rollup untouched, and every produced page is still a
`PENDING` proposal a human approves.

## north-star fit

vouch's load-bearing invariant is "every write goes through a review gate."
this feature adds an llm *drafting* path but keeps the *write* path gated:
each topical page lands as a `PENDING` page proposal via
`proposals.propose_page`, exactly like a hand-filed write or a compiled wiki
page. nothing is auto-approved; `approve()` is never called. the llm decides
how to slice the session, code verifies the slices mechanically, and the human
review is the gate.

it also respects the second invariant the codebase already encodes: session
records are *raw material*, not durable wiki topic pages. the split pages stay
`type: session` — the same feedstock kind `capture.finalize` emits today, just
several topical ones instead of one monolith. `compile.py` remains the sole
citation-verified path from approved claims to durable topic pages, and it
still forbids `session`/`log` as output types. this feature does not erode that
boundary.

## background: the load-bearing constraint

vouch is going multi-host. twelve adapters already exist under `adapters/`
(claude-code, claude-desktop, cline, codex, continue, cursor, generic-mcp,
http-tunnel, jsonl-shell, openclaw, windsurf, zed). the summarizer therefore
cannot be written against claude code's session-end hook, its tool vocabulary,
or its transcript format.

the seam that makes host-neutrality possible already exists: the **observation
buffer**. every host normalizes its native activity into one compact,
host-agnostic shape — `{ts, tool, summary, files?, cmd?}` — written via
`capture.observe` into `.vouch/captures/<session>.jsonl`:

- claude code maps `PostToolUse` payloads via `capture.summarize_tool`.
- codex parses rollout files via `codex_rollout.py` into "the same observation
  shape `capture.observe` produces" and reuses `build_summary_body`.
- openclaw feeds observations through its context-engine ingest.
- mcp-only hosts write observations through the same `observe` contract.

so "summarize a huge session" reads *only* this normalized buffer. the
summarizer never touches a transcript and never assumes a tool name. ingestion
is host-specific and thin; summarization is host-blind.

### the three leaks in the claude-code-first draft (rejected)

the first draft of this design leaked host assumptions in three places, all
removed here:

1. it triggered off claude code's `SessionEnd` hook. → triggers now converge on
   one neutral entry that every host reaches its own way.
2. it pulled the page title from `capture.first_user_prompt`, which parses
   claude code's transcript jsonl. → the session intent now comes from a
   neutral, prioritized source; transcript parsing is a per-host enricher, not
   a core dependency.
3. it framed the whole feature as a `capture.py` (claude-code-flavored)
   concern. → the summarizer is extracted into its own host-blind module.

## design

### architecture

```
  HOST EDGE (per-host, thin)                NEUTRAL CORE (host-blind)
  ────────────────────────                  ─────────────────────────
  claude code  PostToolUse ─┐
  codex        rollout ─────┤   observe()   ┌─ .vouch/captures/<id>.jsonl
  openclaw     ingest ──────┼──────────────►│  {ts,tool,summary,files,cmd}
  cursor/cline/… mcp ───────┘               └─ + optional intent header
                                                        │
                                                        ▼
                                            session_split.summarize(id)
                                              size-gate → mechanical | llm split
                                                        │
                                                        ▼
                                            N PENDING  type:session  proposals
```

### new module: `src/vouch/session_split.py` (host-blind)

one public entry:

```python
summarize(store, session_id, *, intent=None, cwd=None, project=None,
          generated_at=None, mode="auto", config=None) -> dict
```

reads the buffer observations + a git-diff backstop, applies the size gate,
runs the mechanical rollup *or* the llm topical split, files `PENDING`
proposals, deletes the buffer, and writes an audit event. returns
`{captured, summary_proposal_ids, summary_proposal_id, mode, dropped, truncated}`.

`mode` is `"auto"` (gate decides), `"split"` (force llm), or `"mechanical"`
(force the single page). `summary_proposal_id` (first id or `None`) is retained
for backward compatibility with existing `finalize` callers; `summary_proposal_ids`
is the full list.

### new module: `src/vouch/llm_draft.py` (extracted from compile.py)

`run_llm(cmd, prompt, *, timeout_seconds)` + `parse_drafts(raw)` + fence
stripping, run in a throwaway temp cwd with forced utf-8 on both pipe
directions (the locale is latin-1 on some hosts). `compile.py` and
`session_split.py` both import it; `compile.py` keeps its claim-citation
validation. this is a targeted dedup of code both callers need, not a
speculative abstraction.

### `src/vouch/capture.py` (trimmed to ingestion)

keeps `observe`, `summarize_tool`, the buffer helpers, `finalize_all_except`,
and `build_summary_body` (the mechanical summarizer, reused by both branches).
`finalize()` stays as the claude-code / codex-facing wrapper: it resolves the
claude-code transcript intent (its one host-specific enricher), then delegates
to `session_split.summarize`.

### control flow: the three-tier size gate

```
observations + git-diff  →  total = len(obs) + len(changed_files)

  total < min_observations ............... delete buffer, no page      (unchanged)
  mode=auto, total < threshold ........... mechanical single page      (unchanged)
  mode=auto, total ≥ threshold, llm ok ... llm topical split → N pages (new)
  any llm failure / no llm_cmd ........... mechanical single page      (fallback)
```

`threshold_observations` (default 40) sits above `min_observations` (default
3), so small sessions are never handed to an llm. the mechanical rollup is both
the default for mid-size sessions and the universal fallback.

### the split prompt & output contract (host-neutral)

role: "session historian." inlined input:

- the resolved **intent** string, if any
- observation lines rendered `- [<tool>] <summary> (files: …)`, where `<tool>`
  is an opaque label (no claude-code tool-name enum) so any host's vocabulary
  clusters equally
- the git stat, if any
- **taken topics**: existing + pending page names, the same dedupe list
  `compile.py` builds

rules given to the llm: cluster into at most `max_pages` coherent *topics*, one
page each; each page `{title, body}` with an 80–200-word markdown body and a
specific title ("fixed the audit-log write race", not "bug fixes"); **no
`[claim: id]` markers** — their absence is what marks these as uncited
feedstock, distinct from compile's cited pages; output *only* a json array of
`{title, body}` objects, no prose, no code fences.

### validation (mechanical, per draft)

drop-with-reason when: title or body is empty; the title slug collides with an
existing, pending, or in-batch page (the same overwrite-on-approve guard
`compile.py` uses, since `approve()` routes a colliding id through
`update_page`); or the draft is past the `max_pages` cap (cap is first-come, so
the outcome does not depend on drop order). `type` is **forced to `session`** in
code regardless of what the llm emits. survivors are filed:

```python
propose_page(store, title=…, body=…, page_type="session",
             tags=["session", "split"], session_id=session_id,
             metadata={"session_id": session_id},
             proposed_by="session-split",
             rationale="llm topical split of session <id>")
```

`approve()` is never called. the producing agent is already recorded on the
`Session` (`Session.agent`, set at `session_start`) and on the proposal's
`session_id`, so no separate host field is stored on the page.

### "huge" input handling

v1 is a single llm call feeding the whole compact buffer. observation records
are ~30–40 tokens each, so a realistic session — even a very long one — fits a
modern context. guard: if the serialized prompt would exceed `max_input_chars`
(default ~60 000), keep the most-recent observations that fit, append an
explicit `(N older observations elided)` note to the prompt, set `truncated` in
the result, and log it. **no silent cap.** map-reduce chunking (chunk → per-chunk
topic candidates → merge) is documented as a phase-2 upgrade if buffers routinely
exceed the budget; it is deliberately out of scope for v1 to avoid spending two
llm calls on every large session.

### intent resolution (neutral priority order)

1. `Session.task` — the neutral field set at `session_start`
2. an `intent` header record the adapter may write into the buffer
3. a host-registered transcript parser (claude code registers its
   `first_user_prompt`; other hosts need not)
4. a filename/observation-derived fallback

the summarizer core never parses a transcript itself; step 3 is the only seam
where host-specific parsing may run, and it is optional.

### error handling

no resolvable `llm_cmd`, a nonzero exit, a timeout, non-json output, or zero
valid drafts after validation → warn + fall back to the mechanical single page
→ one `PENDING` proposal, `mode="fallback"`. `summarize()` never raises to a
hook and never leaves a session unsummarized. the buffer is deleted only after
a page is filed (or an explicit below-min skip), so a crash mid-run leaves the
buffer intact for the next `finalize-all` sweep to retry.

### audit

new event `session.split`, actor = the triggering human/token label (or the
`session-split` proposer): `object_ids` = the filed proposal ids, `data =
{mode, proposed, dropped, observations, truncated}`. mirrors `compile.run` so
host-triggered splits stay attributable.

### surfaces

- **`kb.summarize_session`** — new neutral method (`session_id`, `mode`), the
  trigger for hosts that cannot run shell hooks. four registration sites the
  `test_capabilities` parity check enforces: mcp tool in `server.py`, jsonl
  handler + `HANDLERS` entry in `jsonl_server.py`, the `METHODS` list in
  `capabilities.py`, and a cli command. write-only, so it attaches no
  `_meta.vouch_salience` sidebar.
- **cli**: `vouch capture finalize` gains `--split/--no-split` (maps to `mode`);
  plus `vouch capture summarize <session_id> [--split/--no-split]`.

### triggers (per-host, one convergent entry)

| host | trigger → calls `session_split.summarize` |
|---|---|
| claude code, codex | existing `vouch capture finalize` hook / ingest |
| openclaw | its `compact` rpc — the "history is huge" signal (fast-follow) |
| mcp-only hosts | new `kb.summarize_session` method |
| all hosts | `finalize-all` stale-buffer sweep on next start (already host-blind) |

### config (neutral, deployment-level)

```yaml
capture:
  split:
    enabled: true
    llm_cmd: null            # defaults to compile.llm_cmd when unset
    threshold_observations: 40
    max_pages: 6
    timeout_seconds: 180
    max_input_chars: 60000
```

## scope

**v1 (this plan):**

- `session_split.py` host-blind core with the three-tier gate and the llm
  topical split
- `llm_draft.py` extracted from `compile.py`; `compile.py` refactored to import it
- `capture.finalize` delegating to the core; `--split/--no-split` cli flag
- `kb.summarize_session` across all four surfaces + parity test
- neutral intent resolution with claude code's transcript parser demoted to an
  optional enricher
- config block + tests

**fast-follow (separate plan):**

- wiring openclaw's `compact` rpc to `session_split.summarize`
- map-reduce chunking for buffers that exceed `max_input_chars`

**out of scope:**

- splitting into durable topic pages (`concept`/`workflow`/`decision`) — that is
  `compile.py`'s job, from cited claims
- an index/parent page linking the children (flat topical pages only)
- auto-approval of any produced page

## testing — `tests/test_session_split.py`

- size gate: `total < threshold` → one mechanical page, `mode="mechanical"`
- size gate: `total ≥ threshold` with a fake `llm_cmd` echoing canned json → N
  `PENDING` proposals, all `type=session`, all pending, `mode="split"`
- fallback: threshold met but no `llm_cmd` → mechanical, `mode="fallback"`
- fallback: llm returns junk / nonzero exit / timeout → mechanical, `mode="fallback"`
- dedupe: a draft whose title collides with an existing page is dropped
- cap: more than `max_pages` drafts → capped, extras in `dropped`
- host-neutral: observations carrying non-claude-code tool names (e.g.
  `fs.write`, `shell.exec`) cluster without crashing
- intent priority: `Session.task` used when present, filename fallback when absent
- back-compat: `finalize()` still returns a `summary_proposal_id` key
- `kb.summarize_session` behavior + `test_capabilities` parity

fake-`llm_cmd` fixture mirrors the existing compile tests: a shell command that
echoes a canned json array on stdout.
