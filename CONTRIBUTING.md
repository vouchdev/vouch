# Contributing to vouch

Thanks for considering a contribution. vouch is pre-1.0 and the surface is
still moving, so a quick conversation in an issue before a large PR will
usually save everyone time.

## Dev setup

\`\`\`bash
git clone https://github.com/vouchdev/vouch
cd vouch
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
\`\`\`

## Day-to-day

\`\`\`bash
make test         # pytest
make lint         # ruff check
make format       # ruff format
make type         # mypy
make check        # lint + type + test
\`\`\`

## Environment variables

| Variable | Values | Default | Purpose |
|---|---|---|---|
| `VOUCH_LOG_FORMAT` | `text` or `json` | `text` | Log output format. `json` emits one JSON object per line with `time`, `level`, `logger`, `message`, and any `extra=` fields merged in at the top level. |
| `VOUCH_LOG_LEVEL` | Any standard level name | `WARNING` | Root logger level. Set to `DEBUG` for verbose output during development. |
| `VOUCH_LOG_FILE` | Filesystem path | unset | Append logs to this file in addition to stderr. Honoured for both `text` and `json` formats. |

Example for local debugging:

\`\`\`bash
VOUCH_LOG_FORMAT=json VOUCH_LOG_LEVEL=DEBUG vouch status
\`\`\`

## Sending a PR

1. Open an issue first for anything that changes the object model, the
   `kb.*` method surface, the on-disk layout, the bundle format, or the
   audit-log shape. These are load-bearing for downstream tools and
   reviewed users — they need a [VEP](proposals/README.md), not just a PR.
2. Branch from `main`. Target `test` for feature branches — `test` is the
   integration branch and reaches `main` via release branches. Keep PRs
   focused; one concern per PR.
3. Add or update tests. New CLI subcommands need a test in `tests/`;
   schema changes need a round-trip test in `tests/test_storage.py` or
   `tests/test_bundle.py`; new env-var behaviour needs a test in
   `tests/test_logging_config.py`.
4. Run `make check` locally before pushing.
5. Update `CHANGELOG.md` under `## [Unreleased]`.
6. PR description should answer: *what changed, why, and what would
   break for someone with an existing `.vouch/` directory?*

## Logging guidelines

- Call `configure_logging()` once at process startup (CLI entry point, MCP
  server, JSONL server). Do not call it inside library code.
- Use `logging.getLogger(__name__)` in every module — never the root logger
  directly.
- Pass structured context via `extra=` rather than string interpolation when
  `VOUCH_LOG_FORMAT=json` consumers may parse the output:

\`\`\`python
logger.info("proposal approved", extra={"proposal_id": pid, "actor": actor})
\`\`\`

- vouch uses `_VouchManagedHandler` (a `logging.StreamHandler` subclass) to
  mark its own handlers. Host applications and test frameworks can add their
  own handlers without risk of them being removed by `configure_logging()`.

## Things we won't merge

- Anything that bypasses the review gate from the agent side
  (e.g. a `kb.write_*` that skips `proposed/`). The whole point is the
  gate; talk to us first if you think you need this.
- Validation relaxations that let claims land without citations. A claim
  without evidence is a `working` note at best — register a source.
- New transports that don't pass the JSONL contract tests
  (`tests/test_jsonl_server.py`) and the capabilities cross-check
  (`tests/test_capabilities.py`).
- Destructive operations on `decided/` or `audit.log.jsonl` outside of a
  bundle-import path. The audit log is append-only by design.

## Style

- Python 3.11+. Lints: ruff (`E`, `F`, `I`, `B`, `UP`, `SIM`, `RUF`).
- Line length 100.
- Prefer pydantic models for any persisted shape — don't write raw dicts
  to YAML.
- No comments unless the *why* is non-obvious. Identifier names should
  carry the *what*.
- Use subclasses as markers in preference to sentinel attributes — it keeps
  `isinstance` checks clean and avoids `type: ignore[attr-defined]`.

## Reporting bugs / asking for features

Use GitHub Issues with the templates under
[.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/). For security issues, see
[SECURITY.md](SECURITY.md) — please don't open a public issue first.
