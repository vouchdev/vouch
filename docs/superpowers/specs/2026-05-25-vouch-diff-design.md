# vouch diff — claim/page revision diff

## Problem

When a claim is superseded or a page is revised, there is no way to see *what
changed* between the old and new artifact. You can `show` each one and compare
by eye, but nothing renders the delta. ROADMAP 0.1 lists `vouch diff
<id-old> <id-new>` for exactly this.

## Goal

`vouch diff <id-old> <id-new>` shows what changed between two claim revisions
or two page revisions — field-level changes plus a line-diff of the long text
field. Read-only: no writes, no proposals, no audit events.

## Decisions

- **Auto-detect kind.** Resolve both ids as claims; if that fails, resolve both
  as pages. Mismatched kinds or unknown ids are a clear error — no `--kind`
  flag.
- **Semantic fields only.** Diff the fields that carry meaning; hide
  always-churning metadata (`id`, `created_at`, `updated_at`,
  `last_confirmed_at`, `approved_by`).
- **Line-diff the long text.** `claim.text` / `page.body` render as a
  `difflib` unified diff; everything else as `field: old → new`.
- **Full `kb.*` parity.** Registered as `kb.diff` at all four sites (MCP tool,
  JSONL handler, `capabilities.METHODS`, CLI) like any other read method —
  see "MCP/JSONL parity" under Non-goals below for the superseded original
  call on this.
- **Omitted `new_id` resolves via `superseded_by`.** For a claim, `new_id` is
  optional; when omitted it resolves to `old_claim.superseded_by`, erroring
  clearly if that's unset. Pages have no successor pointer, so `new_id` is
  required for a page.

## Components — `src/vouch/diff.py`

### `DiffError(Exception)`
Raised for unknown ids and mismatched kinds.

### `FieldChange` (dataclass)
`field: str, old, new` — one changed scalar/list field.

### `ArtifactDiff` (dataclass)
`kind: str, old_id: str, new_id: str, changes: list[FieldChange],
text_diff: list[str]`.

### `diff_artifacts(store, old_id, new_id=None) -> ArtifactDiff`
- **`new_id` resolution:** if omitted, `old_id` must resolve to a claim with
  `superseded_by` set — that becomes `new_id`. A page, or a claim without a
  successor, raises `DiffError` naming the id.
- **Kind resolution:** try `store.get_claim` on both ids → both succeed ⇒
  `kind="claim"`. Otherwise try `store.get_page` on both → `kind="page"`. If an
  id resolves to neither, raise `DiffError("unknown artifact: <id>")`. If one is
  a claim and the other a page, raise
  `DiffError("cannot diff claim against page")`.
- **changes:** for each semantic field whose value differs, append a
  `FieldChange`. The long text field is handled separately (not in `changes`).
- **text_diff:** `list(difflib.unified_diff(old_text.splitlines(),
  new_text.splitlines(), lineterm=""))` for `claim.text` / `page.body`; empty
  when unchanged.

Field sets (long text field rendered as `text_diff`, the rest as changes):
- **Claim** — text *(diff)*; type, status, confidence, evidence, entities,
  tags, supersedes, superseded_by, contradicts, scope.
- **Page** — body *(diff)*; title, type, status, claims, entities, sources,
  tags.

## CLI — `vouch diff OLD [NEW] [--json]`

Follows existing patterns (`_load_store`, `_cli_errors`, `_emit_json`).

Human output:
```
diff claim <old> → <new>
  status: working → stable
  confidence: 0.7 → 0.9
  evidence: ['s1'] → ['s1', 's2']
  text:
    --- a
    +++ b
    -old wording
    +new wording
```
- `--json` → `_emit_json` of the `ArtifactDiff` as a dict.
- No differences → prints `no differences`.

## `kb.diff` — MCP + JSONL

Same read as the CLI, exposed for agents:

- **MCP** `kb_diff(old_id, new_id=None) -> dict` in `server.py`, next to
  `kb_read_claim`/`kb_read_page` in the unrestricted-read section.
- **JSONL** `_h_diff` reads `params["old_id"]` (required) and
  `params["new_id"]` (optional) — `kb.diff` in `HANDLERS`.
- **capabilities** `kb.diff` in `METHODS`, next to `kb.read_relation`.
- Both return `dataclasses.asdict(ArtifactDiff)`.
- Unrestricted like the other by-id read tools (`kb_read_claim`,
  `kb_read_page`) — no `ViewerContext`/scope filtering, since resolving a
  *specific known id* carries the same exposure either way.

## Error handling

- Unknown id (neither claim nor page) → `DiffError` → clean CLI `Error:` line.
- Mismatched kinds → `DiffError` → clean CLI `Error:` line.

## Testing (TDD)

- `diff_artifacts`: two claims differing in status/confidence → matching
  `FieldChange`s; a text change → `text_diff` contains `-`/`+` lines; identical
  claims → empty `changes` and `text_diff`; two pages differing in title/body;
  unknown id → `DiffError`; claim-vs-page → `DiffError`.
- CLI: `vouch diff a b` prints the changed fields; `--json` emits a dict with
  `kind`/`changes`; unknown id → clean `Error:`; identical → `no differences`.

## Non-goals

- Following supersede chains more than one hop (omitted `new_id` resolves one
  `superseded_by` link, not the full chain to the latest revision).
- Diffing entities/relations/sources (claims and pages only, per ROADMAP).
- `ViewerContext` scope filtering on `kb.diff` (see "MCP + JSONL" above —
  matches the other by-id read tools).

Superseded decision from the original design: "MCP/JSONL parity" was
initially scoped out ("CLI-only... does not touch the `kb.*` capability
set"). Issue #327 pointed out this leaves `kb.diff` as the only read method
skipping the four-site registration convention (`CLAUDE.md` §"When you add a
new kb.* method"), so it was added — see "MCP + JSONL" above.
