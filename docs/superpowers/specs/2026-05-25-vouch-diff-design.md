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
- **CLI-only.** Read-only inspection; does not touch the `kb.*` capability set.

## Components — `src/vouch/diff.py`

### `DiffError(Exception)`
Raised for unknown ids and mismatched kinds.

### `FieldChange` (dataclass)
`field: str, old, new` — one changed scalar/list field.

### `ArtifactDiff` (dataclass)
`kind: str, old_id: str, new_id: str, changes: list[FieldChange],
text_diff: list[str]`.

### `diff_artifacts(store, old_id, new_id) -> ArtifactDiff`
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

## CLI — `vouch diff OLD NEW [--json]`

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

- Following supersede chains automatically (caller passes both ids).
- Diffing entities/relations/sources (claims and pages only, per ROADMAP).
- MCP/JSONL parity (`kb.*` surface unchanged).
