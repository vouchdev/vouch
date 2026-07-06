# decision-log/

A vouch KB modelled as a team's decision log. Six decisions across
infrastructure, pricing, and process, each backed by a meeting note
or RFC. Three pages compile them into narratives.

This example exists to answer: *"is vouch suitable for storing
architectural / product decisions, or only for facts an LLM scraped
out of a transcript?"* The answer is yes for either, and this example
shows the former.

## What's here

- 6 claims of `type: decision`, all `status: stable`.
- 3 pages: `decisions-2026-q1.md`, `pricing-policy.md`,
  `process-incidents.md`.
- 5 sources (meeting notes + 2 RFCs).
- An `audit.log.jsonl` showing the gate sequence for each decision.

## Why look at this one

Compared to [tiny/](../tiny/), this example:

- Mixes claim types (decisions and warnings).
- Demonstrates supersession: one claim is superseded by another, with
  the audit trail intact.
- Shows what a real team's `decision_reason` field can look like in
  `decided/` — not just "ok" but the actual rationale.

(Files in this directory follow the same vouch/ layout as
[../tiny/vouch/](../tiny/vouch/). For brevity the README points at
patterns; browse the directory for the full set.)

## See it in action

After `cp -r examples/decision-log/vouch ./.vouch`, here's the fixture in
use. (Images are rendered from the fixture by
[`docs/img/examples/render.py`](../../docs/img/examples/render.py).)

`vouch search postgresql` — both database decisions surface:

<img src="../../docs/img/examples/decision-log-search.svg" alt="vouch search postgresql on the decision-log example" width="720">

`vouch diff` across the two database claims — how the decision evolved,
confidence and text side by side:

<img src="../../docs/img/examples/decision-log-diff.svg" alt="vouch diff showing decision evolution on the decision-log example" width="760">
