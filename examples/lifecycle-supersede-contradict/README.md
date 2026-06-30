# Claim lifecycle: supersede, contradict, archive, confirm

How a claim ages. This runnable example takes four approved claims and walks
each through one lifecycle transition, reading the result back over the JSONL
transport so every status flip is visible. It extends what
[`decision-log/`](../decision-log/) shows for supersede into the full
lifecycle set.

## Run it:

```bash
VOUCH=/path/to/vouch ./run.sh
# or, with vouch on PATH:
./run.sh
```

The script builds a throwaway KB in `$(mktemp -d)`, registers four sources,
proposes four claims as `example-agent`, and approves them as
`human-reviewer` (the review gate forbids self-approval). Then:

1. **supersede** — `c1` ("deploys run on fridays", v1) is superseded by `c2`
   ("deploys run on tuesdays", v2). `c1` flips `working -> superseded`; `c2`,
   the live version, is untouched. The supersede edge is recorded in the
   audit log.
2. **contradict** — `c3` ("cache ttl 60s") and `c4` ("cache ttl 300s") are
   recorded as contradicting. A contradiction relation is written and both
   claims flip to `contested`.
3. **archive** — `c3`, the stale ttl, is archived. It flips to `archived`:
   kept for history and audit, dropped from default retrieval.
4. **confirm** — `c4`, the still-true ttl, is re-confirmed. `last_confirmed_at`
   moves from null to a fresh timestamp so staleness lint resets.

Every status read is a `kb.read_claim` request piped to
`vouch serve --transport jsonl`, so the example also shows the read path.

## Real output excerpt

```text
=== supersede: c1 (fridays, v1) -> c2 (tuesdays, v2) ===
c1 before: working None
superseded deploys-run-on-fridays -> deploys-run-on-tuesdays
c1 after:  superseded None   # status flips to superseded
c2 stays:  working None   # the live version is untouched

=== contradict: c3 (ttl 60s) <-> c4 (ttl 300s) ===
contradiction recorded: the-cache-ttl-is-60-seconds <-> the-cache-ttl-is-300-seconds

=== archive: c3 (the stale ttl) — kept for history, dropped from default retrieval ===
c3 before: contested None
archived the-cache-ttl-is-60-seconds
c3 after:  archived None   # status flips to archived

=== confirm: c4 (still-true ttl) — bumps last_confirmed_at ===
c4 before: contested None
confirmed the-cache-ttl-is-300-seconds
c4 after:  contested 2026-06-30T02:26:30.653083Z   # last_confirmed_at moves forward

=== audit chain is intact ===
why deploys-run-on-fridays (claim)
  approvedBy -> 2a4c7be654fd4c7b9eaf2f8a6fbbfc00 (event)  [...]
  cites -> 77efbfb9a925dcbc363... (source)  [...]
```

Note that `contradict` flips both claims to `contested` — that is why `c3`
reads `contested` (not `working`) just before it is archived, and why `c4`
stays `contested` after being re-confirmed: confirming bumps
`last_confirmed_at` without clearing the contradiction.

## Methods demonstrated

- `kb.supersede` — mark an old claim superseded by a newer one (status flip,
  audit edge).
- `kb.contradict` — record that two claims contradict; both become `contested`.
- `kb.archive` — retire a stale claim; kept for history, omitted from default
  retrieval.
- `kb.confirm` — re-assert a still-true claim; bumps `last_confirmed_at`.
- `kb.read_claim` — read a claim back over the JSONL transport to observe each
  transition.
