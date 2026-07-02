# Tutorials

Step-by-step walkthroughs that take you from zero to a working outcome.
Concrete commands, real output, no abstraction-first jargon. Each tutorial
assumes no prior vouch knowledge and follows the
[Diátaxis](https://diataxis.fr/) tutorial pattern: learning-oriented, walks
you from nothing to a working result in one session, every step produces a
visible change.

Every command shown here was run end-to-end against the current build before
it went into the docs. If a feature isn't verified yet, it's in the
[On the roadmap](#on-the-roadmap) list below — not in a tutorial.

## Shipped

- [**Build your first knowledge base**](first-knowledge-base.md) — the
  canonical solo walkthrough. Initialise a KB in any git repo, register a
  source, propose a claim, hit the review gate (the moment vouch refuses to
  let you approve your own proposal), approve it as a reviewer, then recall it
  with `search`, `context`, and `synthesize`. Ends with the provenance trail:
  `why`, `audit`, `cite`. About 15 minutes, no API keys.

- [**Give your coding agent a reviewed memory**](connect-coding-agent.md) —
  wire vouch into Claude Code, Codex, Cursor, or any of nine MCP hosts with one
  command. Walks the `.mcp.json` + `CLAUDE.md` that `install-mcp` writes, the
  brain-first protocol the agent follows, ambient capture (the agent proposes
  while it works), and the review loop where you approve what lands. About 10
  minutes; needs a KB from the first tutorial.

- [**Share a knowledge base across machines and teammates**](share-a-knowledge-base.md)
  — bundle a reviewed KB into a portable `.tar.gz`, preview the diff before you
  apply it, and import it into another KB with conflict-safe merging. Shows why
  the review gate is a *team* safety property: no contributor can rubber-stamp
  their own writes. Ends with `metrics` and `stats` for observability. About 20
  minutes, no API keys.

- [**Edit your KB as markdown in Obsidian**](edit-in-obsidian.md) — mirror
  approved pages and claims into an Obsidian-compatible vault (wikilinks and
  all, so the graph view works), edit a page in your editor of choice, and
  watch the edit come back as a review-gated proposal. The bytes you changed
  cite themselves so the reviewer sees exactly what triggered the change. About
  15 minutes.

## On the roadmap

These features exist in the CLI today but don't have a full tutorial yet. Run
`vouch <command> --help` for the current surface. Open an issue if one of them
is the walkthrough you need most — that's how the order gets decided.

- **Hybrid + semantic retrieval** — `search` and `context` upgrade from FTS5
  to embedding-backed hybrid ranking when vouch is installed with the
  embeddings extra (`pip install -e '.[embeddings]'`). See
  [`../embeddings.md`](../embeddings.md) and `vouch embeddings stats`.

- **Per-project / per-agent scoping** — `VOUCH_PROJECT` and `VOUCH_AGENT`
  scope what a viewer sees, configured under `retrieval.scope` in
  `config.yaml`. See the scoping block in `vouch capabilities`.

- **Ground a code change in the KB** — `vouch dual-solve`, `vouch auto-pr`,
  and `vouch pr-cache` use the reviewed KB to drive and de-duplicate fixes
  against a GitHub repo. Advanced; start from each command's `--help`.

## Want to write one?

The shipped [`first-knowledge-base.md`](first-knowledge-base.md) is the model:
a concrete scenario, numbered steps that each produce a visible change, real
command output, and a troubleshooting table. If you've used vouch for something
worth walking through, open a PR. Keep every command runnable and verified.

## Related documentation

- **Quickstart:** [`../getting-started.md`](../getting-started.md) — the
  ten-minute version of the first tutorial.
- **Worked example:** [`../example-session.md`](../example-session.md) — a full
  automatic session capture → review → commit → recall loop.
- **Reference:** [`../object-model.md`](../object-model.md) — claims, pages,
  entities, relations, sources. [`../review-gate.md`](../review-gate.md) — the
  gate in depth.
- **Protocol:** [`../../SPEC.md`](../../SPEC.md) — the contract if you're
  writing an alternative server.
- **Per-host setup:** [`../../adapters/`](../../adapters/) — what each MCP host
  adapter writes.
