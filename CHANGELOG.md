# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

### Added
- Seed a cited starter source and claim during `vouch init`, print first-run
  next steps, and document a 30-second onboarding tour (#54).

### Fixed
- Raise `ProposalError("forbidden_self_approval")` in `proposals.approve()` when `approved_by == proposal.proposed_by`, enforcing the review-gate guarantee documented in the README and CONTRIBUTING.
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
