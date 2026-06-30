# Keep a KB healthy: index_rebuild, lint, doctor, source_verify, stats

The operator toolkit. `index_rebuild` rebuilds `state.db` from the durable
files; `lint` surfaces user-actionable problems (broken citations, stale
claims, dangling refs); `doctor` runs the full sweep (lint + source
verification + index check); `source_verify` re-hashes every source and
reports drift; `stats` reports the pending queue, review rates, and citation
coverage. The script seeds two cited claims and then deliberately breaks one
citation, so the health commands have something real to report.

## Run it:

```bash
./examples/maintenance-health/run.sh
# or against a specific binary:
VOUCH=/path/to/vouch ./examples/maintenance-health/run.sh
```

It builds a throwaway KB in `$(mktemp -d)`, runs every command against it,
and cleans up on exit. Nothing touches your real KB.

## The scenario

vouch validates citations at *propose* time, so you can't file a claim that
cites a source that doesn't exist. A dangling citation can only appear after
the fact — a source file gets pruned out from under an already-approved
claim. That is exactly the drift an operator hunts for, so the example
reproduces it: add a runbook source, approve a claim that cites it, then
delete the source file.

After that:

1. `vouch index` rebuilds `state.db` (the derived FTS5 index) from the
   yaml/md files on disk — the durable files are authoritative; `state.db`
   is disposable.
2. `vouch lint --stale-days 30` reports the `broken_citation` and exits `1`
   so it doubles as a CI gate.
3. `vouch doctor` runs the full sweep and prints a `counts` line.
4. `vouch source verify` re-hashes the sources still on disk; they come back
   `stored=ok external=match`. The deleted source is simply gone — its damage
   shows up as the broken citation, not here.
5. `vouch stats --json` reports `coverage_rate` and `broken_citation` so you
   can track corpus health over time.
6. The JSONL transport form (`kb.index_rebuild`) shows the same rebuild over
   the newline-delimited json server that adapters talk to.

## Real output excerpt

```text
=== vouch lint --stale-days 30 — user-actionable findings (exits 1) ===
✗ [broken_citation] claim acme-example-rotates-refresh-tokens-every-24h cites missing 550da2b5...
lint exit code: 1

=== vouch doctor — lint + source verify + index check, with counts ===
✗ [broken_citation] claim acme-example-rotates-refresh-tokens-every-24h cites missing 550da2b5...
-- {'kb_dir': '.../.vouch', 'claims': 3, 'pages': 1, 'sources': 2, ... 'index_present': True}
doctor exit code: 1

=== vouch source verify — re-hash every source still on disk ===
ok  811d6af0b6b5  stored=ok  external=match  .../adr.md
ok  be7aec64b0fc  stored=ok  external=n/a  vouch:init

=== vouch stats --json — citation coverage + broken count ===
{
  "broken_citation": 1,
  "claims_total": 3,
  "claims_with_valid_citation": 2,
  "coverage_rate": 0.6667,
  "invalid_claim": 0
}

=== JSONL transport: kb.index_rebuild ===
{"id": "rebuild-1", "ok": true, "result": {"claims": 3, "pages": 1, "entities": 0, ...}}
```

(The `claims: 3` count includes the starter claim `vouch init` seeds, plus
the two this example adds.)

## A note on self-approval

The script sets `review.approver_role: trusted-agent` in `config.yaml` so the
single example agent can approve its own proposals — otherwise vouch blocks
self-approval (`forbidden_self_approval`). In a real KB a human, or a separate
trusted agent, is the approver. The review gate is never removed; this only
opts one agent into approving.

## Methods demonstrated:

- `kb.index_rebuild` — rebuild `state.db` from durable files (CLI: `vouch index`; JSONL shown)
- `kb.lint` — broken citations, stale claims, dangling refs (CLI: `vouch lint`)
- `kb.doctor` — full health sweep with counts (CLI: `vouch doctor`)
- `kb.source_verify` — re-hash sources, report drift (CLI: `vouch source verify`)
- `kb.stats` — pending queue, review rates, citation coverage (CLI: `vouch stats`)
