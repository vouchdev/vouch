# Contributing to vouch

Thanks for considering a contribution. vouch is a review-gated knowledge
base — the gate is the product — so contributions need to protect that
invariant: writes stay gated, claims stay cited, and the audit log stays
append-only. A quick conversation in an issue before a large PR will
usually save everyone time.

## What happens to your PR

Every PR gets an automated review pass (CodeRabbit; pushes to `test` also
get a Copilot review) plus the CI gate — lint, mypy, the test matrix on
Python 3.11/3.12/3.13, and an sdist + wheel build. A maintainer reads the
review output and the diff, then merges or closes; there is no auto-merge.
So get it green before you push: run the full local gate below, keep the
branch conflict-free, and keep the PR to one concern.

Base feature branches on `test` and target `test` in the PR — it is the
integration branch, and it reaches `main` via promotion PRs cut by the
maintainer. Docs-only changes may target `main` directly when they don't
touch behavior.

## What we welcome

- Anything on [ROADMAP.md](ROADMAP.md).
- Fixes with a regression test that fails on the previous code.
- New host adapters under `adapters/<host>/` (an `install.yaml` manifest —
  see [adapters/README.md](adapters/README.md)).
- Retrieval quality: FTS, embeddings, salience, recall/compile output.
- Capture fidelity, review-console ergonomics, and docs that shorten the
  path to the capture → approve → compile → recall loop.
- Tests, fixtures, CI hardening, and developer-experience improvements.

## What we won't merge

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
- A SaaS / hosted mode. vouch is local-first by design.
- Replacing YAML/markdown with JSON or SQLite as the storage format — the
  diff-in-PRs property requires plaintext (`state.db` stays a derived
  cache).
- A custom config DSL. YAML + pydantic is sufficient.
- Real customer names, internal URLs, or PII in code, fixtures, or docs —
  test fixtures use generic placeholders (`alice-example`, `acme-example`).

## Before opening a PR

- Search existing issues and PRs to avoid duplicate work.
- Open an issue first for anything that changes the object model, the
  `kb.*` method surface, the on-disk layout, the bundle format, or the
  audit-log shape. These are load-bearing for downstream tools and
  reviewed users — they need a [VEP](proposals/README.md), not just a PR.
- Keep the PR narrow. If it spans the server surface, storage, and an
  adapter, explain why it can't be split.
- Don't include secrets, tokens, local absolute paths, or contents of a
  real `.vouch/` directory.

## Dev setup

```bash
git clone https://github.com/vouchdev/vouch
cd vouch
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

## The gate

`make check` is the same gate CI runs. The individual pieces:

```bash
make test         # pytest
make lint         # ruff check
make format       # ruff format
make type         # mypy
make check        # lint + type + test
```

Or exactly what `.github/workflows/ci.yml` runs:

```bash
python -m pytest tests/ -q --ignore=tests/embeddings
python -m mypy src
python -m ruff check src tests
```

The embedding-heavy tests run as a separate CI job: `pip install -e
'.[embeddings]'`, then drop the `--ignore`. If you touch packaging
(`pyproject.toml`, `adapters/` layout, the Dockerfile), also prove the
artifacts: `python -m build`, then check the wheel — CI builds it, but the
wheel-contents and installed-resolution regression tests in
`tests/test_install_adapter.py` are the fast local check.

## Test expectations

Tests should prove behavior, not just exercise lines. The conventions are
strict:

- Tests mirror module names: `tests/test_<module>.py`.
- Every fix ships a regression test that fails on the previous code.
- New CLI subcommands need a test in `tests/`; schema changes need a
  round-trip test in `tests/test_storage.py` or `tests/test_bundle.py`;
  new env-var behaviour needs a test in `tests/test_logging_config.py`.
- A new `kb.*` method has **four registration sites**, and
  `tests/test_capabilities.py` fails if you miss one: the MCP tool in
  `src/vouch/server.py`, the JSONL handler in `src/vouch/jsonl_server.py`,
  the `METHODS` list in `src/vouch/capabilities.py`, and the CLI mirror in
  `src/vouch/cli.py`.
- Fixtures use generic placeholders — never real names, URLs, or PII.

## Required PR contents

- A Conventional Commit-style title: `type(scope): short summary`.
- What changed, why, and — for anything touching storage or migrations —
  *what would break for someone with an existing `.vouch/` directory?*
- The validation commands you ran.
- A `CHANGELOG.md` entry under `## [Unreleased]` for user-visible changes.
- Lowercase prose in the PR body, matching the repo voice. No
  `Co-Authored-By` or AI-attribution trailers in commits.

## Commit and PR titles

```text
feat(compile): cap drafted pages per run
fix(cli): force utf-8 stdio on non-utf-8 locales
docs(contributing): clarify the review gates
chore(release): prepare 1.2.1
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`,
`style`, `build`, `revert`. Scope optional, lowercase, specific. Summary
lowercase, ≤72 characters, no trailing period. Bodies are lowercase prose
explaining the why.

## Release checklist (maintainers)

1. Bump the version in all **four** sites — `pyproject.toml`,
   `openclaw.plugin.json`, `package.json`, `src/vouch/__init__.py` —
   `tests/test_openclaw_plugin_manifest.py` enforces the lockstep.
2. Roll `## [Unreleased]` in `CHANGELOG.md` into a dated `## [X.Y.Z]`
   section.
3. `make check` green; PR titled `chore(release): prepare X.Y.Z` into
   `test`, then promote to `main`.
4. `git tag -a vX.Y.Z && git push origin vX.Y.Z` — `release.yml` builds
   sdist + wheel, publishes to PyPI via Trusted Publishing, pushes the
   `ghcr.io/vouchdev/vouch` images, and creates the GitHub release with
   the CHANGELOG section as the body (title = the annotated tag subject).

## Environment variables

| Variable | Values | Default | Purpose |
|---|---|---|---|
| `VOUCH_LOG_FORMAT` | `text` or `json` | `text` | Log output format. `json` emits one JSON object per line with `time`, `level`, `logger`, `message`, and any `extra=` fields merged in at the top level. |
| `VOUCH_LOG_LEVEL` | Any standard level name | `WARNING` | Root logger level. Set to `DEBUG` for verbose output during development. |
| `VOUCH_LOG_FILE` | Filesystem path | unset | Append logs to this file in addition to stderr. Honoured for both `text` and `json` formats. |

Example for local debugging:

```bash
VOUCH_LOG_FORMAT=json VOUCH_LOG_LEVEL=DEBUG vouch status
```

## Logging guidelines

- Call `configure_logging()` once at process startup (CLI entry point, MCP
  server, JSONL server). Do not call it inside library code.
- Use `logging.getLogger(__name__)` in every module — never the root logger
  directly.
- Pass structured context via `extra=` rather than string interpolation when
  `VOUCH_LOG_FORMAT=json` consumers may parse the output:

```python
logger.info("proposal approved", extra={"proposal_id": pid, "actor": actor})
```

- vouch uses `_VouchManagedHandler` (a `logging.StreamHandler` subclass) to
  mark its own handlers. Host applications and test frameworks can add their
  own handlers without risk of them being removed by `configure_logging()`.

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

Use GitHub Issues. For security issues, see [SECURITY.md](SECURITY.md) —
please don't open a public issue first.
