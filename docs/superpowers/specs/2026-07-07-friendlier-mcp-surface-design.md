# friendlier mcp surface — tool profiles + per-prompt auto-recall — design

date: 2026-07-07
status: approved-direction, pending user review of this spec
decided with user: the main reason vouch feels less user-friendly than pmb is
its mcp surface — 58 tools shoved at the agent every turn. lead with slimming
that surface + auto-recall; search-fusion rides underneath as the quality layer.

## context

a code-grounded comparison against pmb (pmbai.dev, `oleksiijko/pmb`) — the
closest competitor — isolated *why* pmb feels smoother on first touch. it is
almost entirely the mcp surface:

| | pmb | vouch (today) |
|---|---|---|
| tools the agent sees | **10** (default profile) | **58** (all, always) |
| gating mechanism | `PMB_TOOL_PROFILE`, minimal by default | **none** |
| tool names | intent-first (`recall`, `remember`) | mechanic-first (`kb_propose_claim`) |
| recall a memory | 0 tool calls (auto-injected each prompt) | agent must call `kb_context`/`kb_search` |
| context cost | compact one-liners, ~16kb saved | 58 verbose descriptions, paid every turn |

pmb defines **69** tools too — it just exposes 10 by default and hides the
other 59 behind a profile flag (`_toolspec.py`, `server.py:446-478` registers
all then removes those outside the active profile). vouch surfaces its whole
propose→approve→supersede→contradict→synthesize→maintenance lifecycle at once
because the review gate is the product — but the *agent* rarely needs 50 of
those tools, and reading them every turn is the friendliness tax.

the key insight: this is a **presentation** problem, not a capability problem.
the fix touches only which mcp tools are *shown* to a given agent. the protocol
surface, the jsonl and cli surfaces, and — critically — the review gate are all
unchanged.

## scope

one slice, four moves, in priority order:

1. **mcp tool profiles** — a `minimal` default that shows ~7 core tools; the
   rest move to `standard`/`full`. the biggest first-touch win, self-contained.
2. **fusion-by-default retrieval** — wire the already-built `rrf_fuse` into the
   context path so recall quality is good (prerequisite for move 3 feeling
   good). add recency preference + near-duplicate drop.
3. **per-prompt auto-recall** — a `UserPromptSubmit` hook that injects
   relevant context every turn → recall becomes 0 tool calls, like pmb.
4. **compact tool descriptions** — one-line descriptions under non-`full`
   profiles to stop burning context. nice-to-have; lands last.

**out of scope** (later slices): the follow-rate "was it used?" loop and
reproducible A/B numbers (that's the *better-proven* slice); a warm daemon for
sub-50ms injection (only if cold-spawn latency bites); bundling a local
embedding model by default (a packaging decision with its own weight);
auto-*write* of claims from observed actions (pmb does this; for vouch it must
route through the gate as proposals — a separate design).

## move 1 — tool profiles

### the profiles

- **`minimal` (default)** — the everyday knowledge loop, agent-facing:
  `kb_capabilities`, `kb_context`, `kb_search`, `kb_read_page`,
  `kb_propose_claim`, `kb_propose_page`, `kb_status`, `kb_list_pending`.
  (8 tools.) `kb_capabilities` stays in so the agent can always discover the
  wider surface and how to widen its profile.
- **`standard`** — minimal + the review lifecycle for unattended agents:
  `kb_approve`, `kb_reject`, `kb_supersede`, `kb_contradict`, `kb_confirm`,
  `kb_read_claim`, `kb_list_claims`, `kb_neighbors`, `kb_why`. (~16 tools.)
- **`full`** — all 58 (maintenance, provenance, eval, import/export, themes).

approve/reject are deliberately **not** in `minimal`: approval is a human
action done at the cli or review ui, so hiding it from a first-run agent is
correct, not lossy. `standard` exists for the "let an agent run the whole loop"
case.

### mechanism

mirror pmb: register every `@mcp.tool()` as today, then, at server build time,
read the active profile and remove tools outside it from the fastmcp tool
manager (`mcp._tool_manager`). the profile → tool-name sets live in one place —
a new `src/vouch/mcp_profiles.py` (a dict of profile → frozenset of method
names), imported by `server.py`. config precedence: `VOUCH_TOOL_PROFILE` env
var overrides `mcp.tool_profile` in `config.yaml`, default `minimal`.

`capabilities.METHODS` still lists all 58 — the profile filters *exposure*, not
the protocol. `mcp_profiles.py` must be exhaustive: a meta-test asserts every
name in every profile exists in `METHODS`, and that `full` == `METHODS`, so a
new tool can't silently fall out of `full`.

## move 2 — fusion by default

in `context.py:_retrieve`, add a `hybrid` branch that fuses semantic + fts
results via the existing `embeddings/fusion.py:rrf_fuse` instead of the current
first-non-empty waterfall; add `hybrid` to `_VALID_BACKENDS`; flip the
`storage.py` default backend to `hybrid` (gracefully degrading to fts when the
embeddings extra is absent). `auto` — what every already-initialised KB has in
its config — is redefined to mean the same fused path, so existing installs
benefit without a config migration. then, before the context-budget clip: a
greedy near-duplicate (MMR-style) drop so an agent never sees the same fact
twice. (a recency multiplier is deferred to a follow-up — it needs per-hit
timestamps that `_retrieve` does not currently carry.) the CI recall eval
(`eval/recall.py`, `eval.yml`, 0.05 regression floor) is the guardrail —
fusion should raise those numbers and can't regress them.

## move 3 — per-prompt auto-recall

add a `UserPromptSubmit` hook to `adapters/claude-code/.claude/settings.json`
that runs `vouch context "$PROMPT"` with a budget-capped, structured output the
harness injects as additional context. mirror into the cursor adapter and
`install_adapter.py` tiers. **approach: cold spawn first** — it runs once per
turn (not per keystroke), so a few hundred ms is usually invisible; measure it,
and only build the warm daemon (generalizing the existing openclaw rpc) if it
feels laggy. this replaces reliance on the session-start `recall` firehose,
which is not query-relevant.

## move 4 — compact descriptions

under `minimal`/`standard`, serve a one-line description per tool (a
`SHORT_DESC` map, or the docstring's first line) instead of the full docstring;
`full` keeps the complete docstrings. lands last; the tool-count cut is the
bulk of the context win.

## invariants preserved

- **review gate untouched** — no write-path change; propose stays gated;
  approve/reject remain fully available (cli, review ui, `standard`/`full`).
- **git-native yaml, no saas, protocol surface unchanged** — `METHODS`, jsonl,
  and cli keep all 58; only mcp *exposure* narrows.
- **3-surface parity** — the parity test is extended to enumerate the *full*
  mcp tool set + cli commands against `METHODS` (closing the current 2-of-3
  gap as a bonus), checked against `full`, not the active profile.

## verification

- new `tests/test_mcp_profiles.py`: `minimal` exposes exactly the 8; `full`
  exposes all 58; every profile name ⊆ `METHODS`; env var overrides config.
- extended `tests/test_capabilities.py`: full mcp tool set vs `METHODS` (the
  real 3-surface check for the mcp surface).
- recall eval green / improved with fusion (ci-gated).
- manual before/after: fresh claude-code install shows ~8 tools not 58;
  a prompt gets relevant context injected with 0 tool calls; record the
  cold-spawn hook latency.

## risks

- **hidden tool confusion** — an agent (or user) may look for a tool that's in
  `full` but not `minimal`. mitigation: `kb_capabilities` (in `minimal`) and
  the docs list the profiles and how to widen with `VOUCH_TOOL_PROFILE`.
- **fusion regressing recall on some queries** — mitigated by the ci eval
  floor; keep `backend` pinnable in config for escape.
- **hook latency on large KBs** — measured in verification; warm-daemon
  fast-follow is the known escape hatch.
