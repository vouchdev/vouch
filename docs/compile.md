# vouch compile — the llm-wiki ingest pass

`vouch compile` turns approved, cited claims into drafted **topic pages** and
files them as pending proposals. It is the "compile" in *stop re-deriving,
start compiling*: sessions and sources feed claims through the review gate;
compile distills those claims into the small, durable wiki a future agent (or
human) reads first.

```text
     sessions / sources          claims               topic pages
   ───────────────────▶ gate ───────────▶ compile ───────────▶ gate ──▶ wiki
        (capture)             (approve)      (LLM drafts)       (approve)
```

## Setup

Compile needs a deployment-configured LLM command in `.vouch/config.yaml`:

```yaml
compile:
  llm_cmd: "claude -p --model sonnet"   # reads prompt on stdin, prints JSON
  max_pages: 5                          # optional, default 5
  timeout_seconds: 180                  # optional, default 180
```

vouch ships no model dependency; any command that reads a prompt on stdin and
prints JSON on stdout works.

## Run it

```bash
vouch compile               # draft pages, file as pending proposals
vouch compile --dry-run     # show what would be drafted, file nothing
vouch compile --json        # machine-readable report
vouch review                # the human half: approve / reject each draft
```

Or click **compile wiki** in the review UI (`vouch review-ui`) — the button
appears once `compile.llm_cmd` is configured, and the drafts land in the same
queue, marked `by wiki-compiler`.

## What the validator enforces

Drafts are LLM prose, so every citation is verified mechanically before a
proposal is filed. A draft is dropped (and reported) when:

- it lists a claim id that doesn't exist or is retracted,
- its body cites `[claim: x]` for a claim it doesn't list,
- it cites no claims at all,
- a `[[wikilink]]` doesn't resolve to an existing page or a *surviving* page
  in the same batch (drops cascade: if a linked sibling is dropped, the
  linking draft drops too rather than shipping a dangling link),
- its title collides with an existing page or a page draft already pending
  review (`approve()` would route a colliding id through `update_page` and
  silently overwrite — so collisions never reach the queue; this also makes
  re-running compile idempotent instead of queueing duplicates),
- its type is `session` or `log` (raw material, not topics),
- it exceeds `max_pages`.

The compiler proposes; it never approves. Proposals are filed by the
`wiki-compiler` actor (or `VOUCH_AGENT` when set), so under the default gate
the reviewing human is always a different actor and self-approval stays
impossible. Each non-dry run appends a `compile.run` audit event attributed
to whoever triggered it (CLI user, or the review-ui token label), with the
filed proposal ids.

The review UI runs one compile at a time per KB; a second click while one is
running comes back with a "already running" notice instead of stacking LLM
runs.

## Current limits

- **Creates only.** Compiled *updates* to existing pages are a future
  feature; today a colliding draft is dropped at validation (see above) and
  the prompt tells the compiler to skip taken topics.
- One pass over the whole KB. Compile inlines all live claims into the
  prompt; very large KBs will want an incremental "since last compile" mode.
