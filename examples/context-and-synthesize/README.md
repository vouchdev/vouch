# Context packs and cited answers

Two retrieval surfaces an agent injects into a prompt. `kb.context` builds a
budgeted ContextPack — with `--require-citations` / `--min-items` as a quality
gate — and `kb.synthesize` answers a question from approved claims only, with
inline `[cite]` markers. `kb.search` shows the raw hits the pack drew from. The
example also shows the require-citations gate flagging an uncited / empty pack
so the agent declines to inject it.

## Run it

```bash
./examples/context-and-synthesize/run.sh
# or against a specific build:
VOUCH=/path/to/vouch ./examples/context-and-synthesize/run.sh
```

The script builds a throwaway KB in `$(mktemp -d)`, seeds it, runs the three
surfaces, and cleans up on exit. Nothing is written outside the temp dir.

## What it does

1. `vouch init` a fresh KB, then register one source file.
2. Propose four cited claims about an example auth design as `alice-example`
   and approve each as `reviewer-example`. vouch forbids self-approval, so the
   proposer and the approver are different agents — that split *is* the review
   gate.
3. `vouch search "jwt token" --json` — the raw FTS5 hits, scored.
4. `vouch context "jwt token" --limit 5 --max-chars 2000 --require-citations
   --min-items 1` — a budgeted ContextPack. Each item carries a `citations`
   array; `quality.ok` is `true` only when every item is cited and the pack has
   at least `min_items`.
5. `vouch context "billing" --require-citations --min-items 1` — a query with
   no hits yields an empty pack with `quality.ok=false`. The gate doesn't crash;
   it reports the failed check (`min_items`) and the agent declines to inject.
6. `vouch synthesize "jwt token" --depth 5` — an answer assembled from approved
   claims only, each sentence carrying an inline `[claim-id]` citation marker.

## Real output

```text
=== kb.search: raw hits ===
3 hits (backend: fts5)
  - [1.15881022229376] Refresh tokens rotate on every use; the previous «token» is revo
  - [0.3674944995721049] Authentication uses short-lived «JWT» access tokens signed with
  - [0.3549090715045671] «JWT» access tokens expire after 15 minutes and must be refreshe

=== kb.context: budgeted ContextPack with require-citations gate ===
items in pack: 3
budget (max-chars 2000): clipped=0 omitted=0 truncated=False
quality gate ok=True require_citations=True min_items=1
  - Refresh tokens rotate on every use; the previous «token» is revo
      citations: ['cf0b3838e4ad0324b97b5b4c7f4ccc4f4588cd06d1ee3f8de895b08e32f6bbb6']
  ...

=== kb.context: gate flags an uncited / empty pack (quality.ok=false) ===
items: 0
quality.ok: False  (failed checks: ['min_items'])
warnings: ['only 0 items, minimum 1']
agent declines to inject: gate held.

=== kb.synthesize: cited answer (inline markers) ===
Refresh tokens rotate on every use [refresh-tokens-rotate-on-every-use-the-previous-token-is-rev]. Authentication uses short-lived JWT access tokens signed with RS256 [authentication-uses-short-lived-jwt-access-tokens-signed-wit]. JWT access tokens expire after 15 minutes and must be refreshed [jwt-access-tokens-expire-after-15-minutes-and-must-be-refres].

claims cited: 3  gaps: []
```

## Notes

- `vouch context` and `vouch synthesize` always emit JSON; there is no `--json`
  flag. `vouch search` needs `--json` for machine output.
- The `citations` on a context item are source ids (sha256). The `[...]` marker
  in a synthesized answer is the cited claim's id.
- Retrieval here is FTS5, so queries match on tokens that appear in the claims.
  `synthesize` reports anything it couldn't ground in a `gaps` list.

## Methods demonstrated

- `kb.context` — budgeted ContextPack with the `--require-citations` /
  `--min-items` quality gate.
- `kb.synthesize` — cited answer from approved claims only, inline markers.
- `kb.search` — the raw scored hits the pack and answer draw from.
