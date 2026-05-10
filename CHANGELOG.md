# Changelog

All notable changes to vouch are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it reaches 1.0.

## [Unreleased]

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
