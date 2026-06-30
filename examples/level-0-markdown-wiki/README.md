# level-0-markdown-wiki/

Conformance level 0: a plain markdown wiki.

A `vouch/` snapshot at the lowest conformance tier — **pages only, no
claims, no graph**. Just markdown with YAML frontmatter, the way a wiki
or a notes folder looks *before* any review-gated facts exist. This
mirrors AKBP's level-0: the file shapes are vouch's, the conformance
level is the same idea.

The point of this example is to show the floor. A vouch KB does not have
to start with claims, entities, and relations. It can be three wiki
pages someone typed up, sitting in `pages/`, with `claims: []` in every
frontmatter block. The review gate still ran (each page was proposed and
approved — see `audit.log.jsonl`), but no *facts* have been crystallised
into claims yet.

```
vouch/
├── config.yaml
├── audit.log.jsonl          # kb.init + 3 page proposals + 3 approvals
├── schema_version
├── .gitignore               # proposed/ and state.db are derived/local
├── claims/                  # empty — this is the whole point
├── pages/
│   ├── onboarding-a-new-engineer-at-acme-example.md
│   ├── local-development-setup.md
│   └── team-glossary.md
├── decided/
│   ├── 20260630-022916-d3df5b0d.yaml
│   ├── 20260630-022917-09694280.yaml
│   └── 20260630-022917-fb42862e.yaml
├── sources/                 # empty
├── entities/                # empty
└── relations/               # empty
```

## Run it

This is a copy-in snapshot, like [tiny/](../tiny/) — there is no
`run.sh`. Copy the tree into a `.vouch` directory and point a real vouch
binary at it:

```bash
# from a scratch dir
cp -r path/to/examples/level-0-markdown-wiki/vouch .vouch

# 0 claims, 3 pages, no graph
vouch status

# search runs over the page bodies (substring backend until you reindex)
vouch search "staging"
vouch reindex && vouch search "docker postgres"   # fts5 once the index exists

# the two read methods this example demonstrates, over the JSONL transport
printf '%s\n%s\n' \
  '{"id":1,"method":"kb.list_pages","params":{}}' \
  '{"id":2,"method":"kb.read_page","params":{"page_id":"team-glossary"}}' \
  | vouch serve --transport jsonl
```

## What level 0 guarantees

- Every page on disk went through `proposals.approve()`. The write gate
  is intact even with zero claims — browse `audit.log.jsonl` to see the
  `proposal.page.create` → `proposal.page.approve` pairs.
- Pages are plain markdown with YAML frontmatter. You can read them,
  grep them, and review their diffs in a PR.
- `kb.list_pages` and `kb.read_page` work — the read surface is fully
  functional at this tier.

## What level 0 deliberately omits

- **No claims.** Every page has `claims: []`. There are no atomic,
  cited, review-gated facts. Nothing here is something an agent should
  treat as an agreed project fact yet.
- **No graph.** `entities/` and `relations/` are empty. No `[[wikilink]]`
  edges between pages, no entity model.
- **No sources.** `sources/` is empty; pages cite nothing because pages
  at this level are narrative, not evidence-backed claims.
- **No semantic index shipped.** `state.db` is derived and gitignored;
  `vouch status` reports `index: missing` until you `vouch reindex`.
  Search still works via the substring fallback.

To climb to the next level you would propose a claim (`vouch
propose-claim --text … --evidence …`), register the source it cites,
and approve it — at which point `claims/` stops being empty and the KB
is no longer level 0.

## Real output

```
$ vouch status
KB at /tmp/.../.vouch
  durable: 0 claims  •  3 pages  •  0 sources  •  0 entities  •  0 relations
  pending: 0 proposals
  audit:   7 events  •  index: missing

$ vouch search "staging"
page/onboarding-a-new-engineer-at-acme-example  Onboarding a new engineer at acme-example  (substring)
page/team-glossary                              Team glossary  (substring)

$ printf '%s\n' '{"id":1,"method":"kb.list_pages","params":{}}' | vouch serve --transport jsonl
# result.ids -> ['local-development-setup',
#                'onboarding-a-new-engineer-at-acme-example',
#                'team-glossary']
```

## Methods demonstrated

- `kb.list_pages` — enumerate every page in the KB.
- `kb.read_page` — fetch one page (frontmatter + body) by id.

(Also exercised on the side: `vouch status`, `vouch search`, `vouch
reindex` — but the load-bearing read surface for level 0 is the two
methods above.)

## What this example is *not*

- It is not a teaching example for claims, evidence, or the graph. By
  design it has none. See [tiny/](../tiny/) for claims + sources, and
  [decision-log/](../decision-log/) for supersession and a richer
  audit trail.
- The page data is generic placeholder content (`acme-example`,
  `alice-example` as the approving reviewer). No real names, URLs, or
  PII.
