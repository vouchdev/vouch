# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- `vouch sync-check` and `vouch sync-apply` reconcile another `.vouch`
  directory or bundle by importing only non-conflicting durable artifacts and
  reporting conflicts without overwriting reviewed knowledge.
- `vouch pending --json` emits pending proposals as structured JSON for shell
  scripts, CI checks, and multi-agent review dashboards.
- `vouch diff <id-old> <id-new>` shows what changed between two claim revisions or two page revisions — field-level changes plus a line-diff of the long text/body. Auto-detects the artifact kind and hides always-churning metadata. Read-only; supports `--json`.
- Seed a cited starter source and claim during `vouch init`, print first-run
  next steps, and document a 30-second onboarding tour (#54).
- Add `vouch review`, a guided CLI queue for approving, rejecting, skipping,
  or dry-running pending proposals without bypassing the review gate.

### Fixed
- Add `put_relation_idempotent()` to `KBStore` and use it in `supersede()` and `contradict()` so retrying after a partial failure converges to a consistent state instead of raising `ValueError`.
- Raise `ProposalError("forbidden_self_approval")` in `proposals.approve()` when `approved_by == proposal.proposed_by`, enforcing the review-gate guarantee documented in the README and CONTRIBUTING.
- `crystallize()` now sets `review.approver_role: trusted-agent` context so single-agent sessions can be crystallized without hitting the `forbidden_self_approval` guard (#47).
- Bundle import rejects tar members whose path escapes `kb_dir`
  (CVE-2007-4559, #9). Previously a crafted `.tar.gz` with a member
  named `../../evil.txt` could write outside `.vouch/`; the manifest
  allow-list did not prevent this because the manifest lives inside
  the same tarball. `import_apply`, `import_check`, and `export_check`
  now validate every member path and raise on unsafe names.
- Fix `vouch search` CLI: assign backend label per code path so substring fallback results are no longer mislabelled as `fts5`; update stale docstring to reflect multi-backend search surface (#52).
- `vouch crystallize` now indexes its session-summary page into FTS5 so it
  surfaces from `vouch search` / `kb.search` / `kb.context` without a
  `vouch index` rebuild. Previously the summary was written via
  `store.put_page()` only, so on KBs with a populated `state.db` it was
  silently absent from search results (#60).
- Bundle export uses POSIX `/` separators in `manifest.json` and tar member
  names on every platform. Previously on Windows the manifest stored
  `sources\<sha>\meta.yaml` while the tarball stored `sources/<sha>/meta.yaml`,
  so `vouch export-check` returned `ok: false` on the bundle vouch had just
  produced, `manifest.counts` was always zero, and `vouch import-apply` was
  a silent no-op. Existing Linux/macOS bundles are unchanged (their paths
  were already POSIX); Windows bundles produced before this fix should be
  re-exported.
- `bundle.import_check` and `bundle.import_apply` now verify each tar
  member's `sha256` against `manifest.json` (#74). Previously the
  per-file hash was only enforced by `export_check`; the import side
  trusted any tar member whose path appeared in the manifest, so a
  tampered tarball with an unchanged manifest could land
  attacker-controlled content into the KB while the audit log
  recorded a clean `bundle.import` event. `import_apply` re-verifies
  at write time and raises on mismatch, so a bundle that is tampered
  with between `import_check` and the apply re-open is rejected
  before anything reaches disk and the audit log does not record
  a `bundle.import` event.

## [0.0.1] — 2026-05-17

Initial alpha. Surface intentionally small; expect breaking changes pre-1.0.

### Added
- Object model: `Source`, `Evidence`, `Claim`, `Page`, `Entity`, `Relation`,
  `Session`, `Proposal`, `AuditEvent`.
- `.vouch/` repository layout with `claims/`, `pages/`, `sources/`,
  `entities/`, `relations/`, `evidence/`, `sessions/`, `proposed/`,
  `decided/`, `audit.log.jsonl`, `state.db`, `config.yaml`.
- Review gate: agents file proposals; humans approve with `vouch approve`.
  `proposed/` gitignored so rejected drafts never enter history.
- CLI: `init`, `discover`, `capabilities`, `status`, `lint`, `doctor`,
  `pending`, `show`, `approve`, `reject`, `propose-*`, `source add/verify`,
  `supersede`, `contradict`, `archive`, `confirm`, `cite`,
  `session start/end`, `crystallize`, `search`, `context`, `index`,
  `audit`, `export`, `export-check`, `import-check`, `import-apply`,
  `serve`.
- Transports: MCP over stdio and newline-delimited JSON (JSONL).
- SQLite FTS5 index, rebuildable from files.
- Append-only audit log, JSONL.
- Portable bundle export/import (tar.gz + manifest with per-file sha256).
- Content-addressed sources; evidence registration de-duplicates.
- Claim validation: at least one source/evidence citation required.
- Per-agent attribution via `VOUCH_AGENT` env var.

[Unreleased]: https://github.com/plind-junior/vouch/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/plind-junior/vouch/releases/tag/v0.0.1
