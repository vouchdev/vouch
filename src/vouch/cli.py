"""vouch CLI.

Agents propose writes via the MCP / JSONL servers; humans use this CLI to
review, lifecycle-manage, lint, export and import. All surfaces share the
same storage + audit + index layer.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import click
import yaml

from . import __version__, bundle, health, volunteer_context
from . import audit as audit_mod
from . import capture as capture_mod
from . import codex_rollout as codex_rollout_mod
from . import compile as compile_mod
from . import digest as digest_mod
from . import fetch as fetch_mod
from . import inbox as inbox_mod
from . import install_adapter as install_mod
from . import lifecycle as life
from . import metrics as metrics_mod
from . import migrations as migrations_mod
from . import notify as notify_mod
from . import pr_cache as prc_mod
from . import provenance as prov_mod
from . import recall as recall_mod
from . import sessions as sess_mod
from . import stats as stats_mod
from . import sync as sync_mod
from . import synthesize as synth
from . import trust as trust_mod
from . import vault_sync as vault_sync_mod
from . import verify as verify_mod
from .capabilities import capabilities as build_caps
from .context import build_context_pack
from .lifecycle import LifecycleError
from .logging_config import configure_logging
from .models import Proposal, ProposalKind, ProposalStatus
from .onboarding import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    available_templates,
    seed_starter_kb,
)
from .page_filters import filter_pages, parse_kv
from .page_kinds import PageKindError, load_page_kind_registry
from .proposals import (
    EXPIRE_ACTOR,
    ProposalError,
    check_approvable,
    expire_pending,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
    reject_auto_extracted,
)
from .proposals import (
    approve as do_approve,
)
from .proposals import (
    reject as do_reject,
)
from .storage import (
    ArtifactNotFoundError,
    KBNotFoundError,
    KBStore,
    discover_root,
)


@contextmanager
def _cli_errors() -> Iterator[None]:
    # Translate domain errors into click.ClickException so users see a
    # one-line `Error: ...` instead of a Python traceback. Without this,
    # ProposalError / LifecycleError (both RuntimeError subclasses) escape
    # the narrower `(ArtifactNotFoundError, ValueError)` tuples previously
    # used per-command. The MCP and JSONL servers do the equivalent in
    # their own request envelopes.
    try:
        yield
    except (
        ArtifactNotFoundError,
        ValueError,
        ProposalError,
        LifecycleError,
        migrations_mod.MigrationError,
        codex_rollout_mod.CodexRolloutError,
    ) as e:
        raise click.ClickException(str(e)) from e


def _load_store(start: Path | None = None) -> KBStore:
    try:
        return KBStore(discover_root(start))
    except KBNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        click.echo("hint: run `vouch init` in your project root.", err=True)
        sys.exit(2)


def _whoami() -> str:
    # Match MCP/JSONL server behaviour (server.py, jsonl_server.py): when an
    # agent invokes the CLI it sets VOUCH_AGENT; honour it as the actor so
    # multi-agent attribution stays consistent across transports. VOUCH_USER
    # remains an escape hatch; OS user is the friendly default for humans.
    return os.environ.get("VOUCH_AGENT") or os.environ.get("VOUCH_USER") or getpass.getuser()


def _emit_json(obj) -> None:
    with trust_mod.trust_context(trust_mod.CLI):
        if isinstance(obj, dict):
            obj = trust_mod.attach_trust(obj)
    click.echo(json.dumps(obj, indent=2, default=str, sort_keys=True))


def _color_enabled() -> bool:
    # Honour the de-facto conventions: NO_COLOR disables, FORCE_COLOR forces,
    # otherwise colour only when stdout is an interactive terminal. Keeps pipes,
    # CI, and `--json` output clean while still being pretty in a shell.
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _style(text: str, **kwargs: Any) -> str:
    return click.style(text, **kwargs) if _color_enabled() else text


def _echo(message: str = "", *, err: bool = False) -> None:
    # click.echo strips ANSI when the stream isn't a TTY unless told otherwise;
    # pass an explicit color flag so FORCE_COLOR into a pipe keeps the styling
    # and NO_COLOR / plain pipes stay clean.
    click.echo(message, err=err, color=_color_enabled())


def _force_utf8_stdio() -> None:
    # On non-UTF-8 locales (e.g. LANG=en_US.ISO-8859-1) Python encodes stdio
    # with the locale codec, so the '•' / '…' in CLI output — and any
    # non-ASCII KB content flowing through the stdio servers — raises
    # UnicodeEncodeError. Artifacts are UTF-8 on disk; speak UTF-8 on the
    # wire too, replacing rather than crashing for terminals that can't.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if (
            isinstance(stream, io.TextIOWrapper)
            and (stream.encoding or "").lower().replace("-", "") != "utf8"
        ):
            with suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


# At import, not in the cli() callback: click renders eager --help /
# --version output during argument parsing, before any group callback
# runs — and the group docstring's em dash already crashes a latin-1
# stdout. Idempotent and a no-op on utf-8 streams.
_force_utf8_stdio()


@click.group()
@click.version_option(__version__, prog_name="vouch")
def cli() -> None:
    """vouch — git-native, review-gated knowledge base for LLM agents."""
    configure_logging()


# --- bootstrap ------------------------------------------------------------


@cli.command()
@click.option("--path", default=".", type=click.Path(file_okay=False), show_default=True)
@click.option(
    "--template",
    default=DEFAULT_TEMPLATE,
    show_default=True,
    type=click.Choice(available_templates()),
    help="Seed preset applied on top of the starter KB.",
)
def init(path: str, template: str) -> None:
    """Initialise a .vouch/ knowledge base at PATH."""
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = KBStore.init(root)
    seed = seed_starter_kb(store, approved_by=_whoami())
    template_result = None
    if template != DEFAULT_TEMPLATE:
        template_result = TEMPLATES[template](store, approved_by=_whoami())
    health.rebuild_index(store)
    audit_mod.log_event(store.kb_dir, event="kb.init", actor=_whoami())
    click.echo(f"Initialised KB at {store.kb_dir}")
    if seed.created_anything:
        click.echo(f"Seeded starter claim: {seed.claim_id}")
    else:
        click.echo("Starter claim already present.")
    if template_result is not None:
        if template_result.created_anything:
            click.echo(
                f"Applied template '{template_result.template}': "
                f"{len(template_result.created)} item(s) created"
            )
        else:
            click.echo(f"Template '{template_result.template}' already applied.")
    click.echo("Next steps:")
    click.echo("  vouch status")
    click.echo("  vouch search agent")
    click.echo("  vouch serve")


@cli.command()
@click.option("--path", default=".", show_default=True)
def discover(path: str) -> None:
    """Walk up from PATH and print the nearest .vouch/ root, or fail."""
    try:
        root = discover_root(Path(path))
        _emit_json({"root": str(root), "kb_dir": str(root / ".vouch")})
    except KBNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)


@cli.command()
def capabilities() -> None:
    """Emit the JSON capabilities descriptor (mirrors kb.capabilities)."""
    _emit_json(build_caps().model_dump(mode="json"))


# --- status / health ------------------------------------------------------


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def status(as_json: bool) -> None:
    """Show artifact counts + pending proposals."""
    store = _load_store()
    s = health.status(store)
    if as_json:
        _emit_json(s)
        return
    click.echo(f"KB at {s['kb_dir']}")
    click.echo(
        f"  durable: {s['claims']} claims  •  {s['pages']} pages  •  "
        f"{s['sources']} sources  •  {s['entities']} entities  •  "
        f"{s['relations']} relations"
    )
    pending = s["pending_proposals"]
    pending_str = _style(str(pending), fg="yellow" if pending else "green")
    _echo(f"  pending: {pending_str} proposals")
    present = s["index_present"]
    index_str = _style("present", fg="green") if present else _style("missing", fg="red")
    _echo(f"  audit:   {_style(str(s['audit_events']), fg='cyan')} events  •  index: {index_str}")


@cli.command()
@click.option(
    "--days",
    default=30,
    show_default=True,
    type=int,
    help="Review decision window (days). Use 0 for all-time.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def stats(days: int, as_json: bool) -> None:
    """KB observability: pending queue, review rates, citation coverage."""
    store = _load_store()
    since = None if days == 0 else days
    body = stats_mod.collect_stats(store, since_days=since)
    if as_json:
        _emit_json(body)
        return
    _echo(f"KB at {_style(str(body['kb_dir']), bold=True)}")
    pending = body["pending"]
    _echo(
        f"  pending: {_style(str(pending['total']), fg='yellow' if pending['total'] else 'green')} "
        f"proposal(s)"
    )
    if pending["by_agent"]:
        for agent, count in pending["by_agent"].items():
            _echo(f"    {agent}: {count}")
    age = pending["age_days"]
    if age["median"] is not None:
        _echo(
            f"  pending age (days): median {age['median']}, max {age['max']}"
            + (f" ({age['oldest_id']})" if age["oldest_id"] else "")
        )
    review = body["review"]
    window = "all time" if review["window_days"] is None else f"last {review['window_days']}d"
    _echo(
        f"  review ({window}): "
        f"{review['approved']} approved, {review['rejected']} rejected, "
        f"{review['expired']} expired"
    )
    rate = review["approval_rate"]
    if rate is not None:
        _echo(f"  approval rate: {_style(f'{rate * 100:.1f}%', fg='cyan')}")
    cites = body["citations"]
    cov = cites["coverage_rate"]
    cov_str = f"{cov * 100:.1f}%" if cov is not None else "n/a"
    _echo(
        f"  citations: {cites['claims_with_valid_citation']}/{cites['claims_total']} "
        f"claims with valid citations ({cov_str})"
    )
    if cites["invalid_claim"] or cites["broken_citation"]:
        _echo(f"    invalid: {cites['invalid_claim']}, broken: {cites['broken_citation']}")


@cli.command()
@click.option(
    "--days",
    default=365,
    show_default=True,
    type=click.IntRange(min=0),
    help="Window (local calendar days). Use 0 for all-time.",
)
@click.option(
    "--tz-offset-minutes",
    default=0,
    show_default=True,
    type=int,
    help="Viewer's UTC offset in minutes for local-time bucketing.",
)
@click.option("--tz", default=None, help="IANA zone for local-time bucketing (wins over offset).")
@click.option("--project", default=None, help="Viewer project for audit scope filtering.")
@click.option("--agent", default=None, help="Viewer agent for audit scope filtering.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def activity(
    days: int,
    tz_offset_minutes: int,
    tz: str | None,
    project: str | None,
    agent: str | None,
    as_json: bool,
) -> None:
    """Audit activity buckets: per-day counts, hour-of-week matrix, actors."""
    from .scoping import viewer_from

    store = _load_store()
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    body = stats_mod.collect_activity(
        store, days=days, tz_offset_minutes=tz_offset_minutes, tz=tz, viewer=viewer,
    )
    if as_json:
        _emit_json(body)
        return
    window = "all time" if body["window_days"] is None else f"last {body['window_days']}d"
    _echo(
        f"activity ({window}): {_style(str(body['total_events']), fg='cyan')} events "
        f"on {body['active_days']} day(s)"
    )
    if body["first_event_day"]:
        _echo(f"  span: {body['first_event_day']} → {body['last_event_day']}")
    for actor, count in list(body["by_actor"].items())[:8]:
        _echo(f"  {actor}: {count}")


@cli.command(name="digest")
@click.option(
    "--since",
    default=digest_mod.DEFAULT_SINCE_SPEC,
    show_default=True,
    help="Window: a duration (7d, 12h), an ISO date, or 'all'.",
)
@click.option(
    "--stale-days",
    default=metrics_mod.DEFAULT_STALE_DAYS,
    show_default=True,
    type=int,
    help="Freshness threshold for the stale-claims section.",
)
@click.option(
    "--limit",
    default=digest_mod.DEFAULT_LIMIT,
    show_default=True,
    type=int,
    help="Cap per section (pending, decisions, stale, followups).",
)
@click.option(
    "--format",
    "fmt",
    default="text",
    show_default=True,
    type=click.Choice(["text", "json", "markdown"]),
)
def digest_cmd(since: str, stale_days: int, limit: int, fmt: str) -> None:
    """Read-only briefing: pending queue, recent decisions, stale claims,
    followups due. Writes nothing — safe to run from cron."""
    store = _load_store()
    try:
        since_dt = metrics_mod.parse_since(since)
    except metrics_mod.MetricsError as e:
        raise click.UsageError(str(e)) from e
    d = digest_mod.build(
        store, since=since_dt, stale_after_days=stale_days, limit=limit,
    )
    if fmt == "json":
        _emit_json(d.to_dict())
    elif fmt == "markdown":
        click.echo(digest_mod.render_markdown(d))
    else:
        click.echo(digest_mod.render_text(d))


def _findings_json(report) -> list[dict[str, Any]]:
    return [
        {
            "severity": f.severity,
            "code": f.code,
            "message": f.message,
            "object_ids": list(getattr(f, "object_ids", []) or []),
        }
        for f in report.findings
    ]


@cli.command()
@click.option("--stale-days", default=180, show_default=True, type=int)
def lint(stale_days: int) -> None:
    """Surface user-actionable problems: broken citations, stale claims, dangling refs."""
    store = _load_store()
    report = health.lint(store, stale_after_days=stale_days)
    for f in report.findings:
        marker = {"error": "✗", "warning": "!", "info": "·"}.get(f.severity, "?")
        click.echo(f"{marker} [{f.code}] {f.message}")
    if not report.findings:
        click.echo("clean")
    sys.exit(0 if report.ok else 1)


@cli.command()
def doctor() -> None:
    """Full health sweep: lint + source verification + index check."""
    store = _load_store()
    report = health.doctor(store)
    for f in report.findings:
        marker = {"error": "✗", "warning": "!", "info": "·"}.get(f.severity, "?")
        click.echo(f"{marker} [{f.code}] {f.message}")
    click.echo(f"-- {report.counts}")
    sys.exit(0 if report.ok else 1)


@cli.command()
def fsck() -> None:
    """Deep consistency check: orphan embeddings, dangling supersede/contradict
    chains, decided-proposal ↔ artifact mismatches, index-vs-file drift.
    """
    store = _load_store()
    report = health.fsck(store)
    for f in report.findings:
        marker = {"error": "✗", "warning": "!", "info": "·"}.get(f.severity, "?")
        line = f"{marker} [{f.code}] {f.message}"
        if f.object_ids:
            line += f" (objects: {', '.join(f.object_ids)})"
        click.echo(line)
    if not report.findings:
        click.echo("clean")
    sys.exit(0 if report.ok else 1)


@cli.group(invoke_without_command=True)
@click.option("--check", "check_only", is_flag=True, help="Only check if a migration is needed.")
@click.option("--dry-run", is_flag=True, help="Show planned format changes without writing.")
@click.option("--to-version", type=int, default=None, help="Target KB format (integer) version.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
@click.pass_context
def migrate(
    ctx: click.Context,
    check_only: bool,
    dry_run: bool,
    to_version: int | None,
    as_json: bool,
) -> None:
    """Migrate the KB.

    With no subcommand, runs the legacy integer *format* migration of the
    .vouch/ directory layout. The subcommands (status / plan / apply / rollback
    / verify) drive the semver model-schema migrations keyed off
    .vouch/schema_version.
    """
    if ctx.invoked_subcommand is not None:
        return
    if check_only and dry_run:
        raise click.ClickException("--check and --dry-run are mutually exclusive")

    store = _load_store()
    with _cli_errors():
        result = migrations_mod.migrate(
            store,
            to_version=to_version,
            dry_run=check_only or dry_run,
        )
        if as_json:
            _emit_json(asdict(result))
        else:
            if result.steps:
                click.echo(
                    f"KB format: {result.from_version} -> {result.to_version} "
                    f"({'dry run' if result.dry_run else 'applied'})"
                )
                for step in result.steps:
                    click.echo(f"- {step}")
                for change in result.changes:
                    click.echo(f"  * {change}")
            else:
                click.echo(f"KB format: {result.from_version} (up to date)")

        if result.applied:
            health.rebuild_index(store)
            audit_mod.log_event(
                store.kb_dir,
                event="kb.migrate",
                actor=_whoami(),
                reversible=False,
                data={
                    "from_version": result.from_version,
                    "to_version": result.to_version,
                    "steps": result.steps,
                    "changes": result.changes,
                },
            )

    if check_only and result.steps:
        sys.exit(1)


@migrate.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate_status(as_json: bool) -> None:
    """Show the KB schema version, target, and pending migrations."""
    store = _load_store()
    with _cli_errors():
        result = migrations_mod.schema_status(store)
    if as_json:
        _emit_json(result)
        return
    click.echo(f"schema: {result['schema_version']} -> {result['target_version']}")
    if result["up_to_date"]:
        click.echo("up to date")
    else:
        click.echo(f"pending: {', '.join(result['pending'])}")


@migrate.command("plan")
@click.option("--to", "to_version", default=None, help="Target schema version (semver).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate_plan(to_version: str | None, as_json: bool) -> None:
    """Dry-run: list every file each pending migration would change."""
    store = _load_store()
    with _cli_errors():
        result = migrations_mod.schema_plan(store, to_version=to_version)
    if as_json:
        _emit_json(result)
        return
    if not result["needed"]:
        click.echo(f"schema: {result['current_version']} (up to date)")
        return
    click.echo(f"schema: {result['current_version']} -> {result['target_version']}")
    for step in result["steps"]:
        click.echo(
            f"- {step['manifest_id']}: {step['from_version']} -> {step['to_version']} "
            f"({step['artifact']}, {step['file_count']} file(s))"
        )
        for rel in step["changed"]:
            click.echo(f"  * {rel}")
    click.echo(f"total: {result['total_files']} file(s)")


@migrate.command("apply")
@click.option("--to", "to_version", default=None, help="Target schema version (semver).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt (CI).")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate_apply(to_version: str | None, yes: bool, as_json: bool) -> None:
    """Apply pending schema migrations (audit-logged, atomic, reversible)."""
    store = _load_store()
    with _cli_errors():
        preview = migrations_mod.schema_plan(store, to_version=to_version)
        if not preview["needed"]:
            if as_json:
                _emit_json(
                    {
                        "applied": False,
                        "from_version": preview["current_version"],
                        "to_version": preview["current_version"],
                        "manifests": [],
                        "files": 0,
                    }
                )
            else:
                click.echo(f"schema: {preview['current_version']} (up to date)")
            return
        if not yes:
            click.confirm(
                f"Apply {len(preview['steps'])} migration(s) "
                f"({preview['current_version']} -> {preview['target_version']}, "
                f"{preview['total_files']} file(s))?",
                abort=True,
            )
        result = migrations_mod.schema_apply(store, to_version=to_version, actor=_whoami())
        # state.db is derived; rebuild it under the new layout.
        health.rebuild_index(store)
    if as_json:
        _emit_json(result)
    else:
        click.echo(
            f"schema: {result['from_version']} -> {result['to_version']} "
            f"({result['files']} file(s), {len(result['manifests'])} manifest(s))"
        )


@migrate.command("rollback")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate_rollback(as_json: bool) -> None:
    """Reverse the most recently applied schema migration."""
    store = _load_store()
    with _cli_errors():
        result = migrations_mod.schema_rollback(store, actor=_whoami())
        health.rebuild_index(store)
    if as_json:
        _emit_json(result)
    else:
        click.echo(
            f"schema: {result['from_version']} -> {result['to_version']} "
            f"(rolled back {result['files']} file(s))"
        )


@migrate.command("verify")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate_verify(as_json: bool) -> None:
    """Parse-load every artifact under the current schema version."""
    store = _load_store()
    with _cli_errors():
        result = migrations_mod.schema_verify(store)
    if as_json:
        _emit_json(result)
    elif result["ok"]:
        click.echo(f"verified {result['checked']} artifact(s) at schema {result['schema_version']}")
    else:
        click.echo(f"FAILED: {len(result['errors'])} of {result['checked']} artifact(s)")
        for err in result["errors"]:
            click.echo(f"  ✗ {err['path']}: {err['error']}")
    if not result["ok"]:
        sys.exit(1)


# --- metrics --------------------------------------------------------------


def _fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _fmt_secs(x: float | None) -> str:
    """Human-friendly duration for the table; raw seconds stay in --json."""
    if x is None:
        return "—"
    if x < 90:
        return f"{x:.0f}s"
    if x < 5400:
        return f"{x / 60:.1f}m"
    if x < 172800:
        return f"{x / 3600:.1f}h"
    return f"{x / 86400:.1f}d"


@cli.command()
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the stable JSON schema (see docs/metrics.md)."
)
@click.option(
    "--prometheus",
    "as_prom",
    is_flag=True,
    help="Emit Prometheus textfile-collector format "
    "(write to <textfile_dir>/vouch.prom from a sidecar).",
)
@click.option(
    "--since",
    default=None,
    help="Window the audit log: a duration like 30d / 12h / 2w, "
    "an ISO date like 2026-01-01, or 'all' (default: all).",
)
@click.option("--until", default=None, help="Upper bound for the window (same formats as --since).")
@click.option(
    "--stale-days",
    default=metrics_mod.DEFAULT_STALE_DAYS,
    show_default=True,
    type=int,
    help="A claim un-confirmed for this many days counts as stale "
    "(matches `vouch lint --stale-days`).",
)
@click.option(
    "--top",
    "top_actors",
    default=metrics_mod.DEFAULT_TOP_ACTORS,
    show_default=True,
    type=int,
    help="How many actors to show in the leaderboard (0 = all).",
)
def metrics(
    as_json: bool,
    as_prom: bool,
    since: str | None,
    until: str | None,
    stale_days: int,
    top_actors: int,
) -> None:
    """Observability for the review gate + corpus (vouchdev/vouch#192).

    \b
    Examples:
      vouch metrics                 # human table, all of history
      vouch metrics --json          # stable schema for a Prometheus sidecar
      vouch metrics --prometheus    # textfile-collector exposition
      vouch metrics --since 30d     # only the last 30 days of the audit log
      vouch metrics --since 2026-01-01 --until 2026-02-01

    All numbers derive purely from .vouch/audit.log.jsonl + the artifact files
    — no new on-disk state. The --json shape is documented and stable.
    """
    if as_json and as_prom:
        raise click.ClickException("choose one of --json / --prometheus, not both")

    store = _load_store()
    try:
        since_dt = metrics_mod.parse_since(since)
        until_dt = metrics_mod.parse_since(until)
        m = metrics_mod.compute(
            store,
            since=since_dt,
            until=until_dt,
            stale_after_days=stale_days,
            top_actors=top_actors,
        )
    except metrics_mod.MetricsError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        _emit_json(m.to_dict())
        return
    if as_prom:
        click.echo(metrics_mod.render_prometheus(m), nl=False)
        return

    # --- human table ---
    window = "all history" if m.since is None else f"since {m.since.isoformat()}"
    if m.until is not None:
        window += f" until {m.until.isoformat()}"
    click.echo(f"vouch metrics  ({window})")
    click.echo("")
    click.echo("  review gate")
    click.echo(f"    proposals created   {m.proposals_created}")
    click.echo(f"    approved / rejected {m.approvals} / {m.rejections}")
    click.echo(f"    approval rate       {_fmt_pct(m.approval_rate)}")
    if m.approval_rate_by_kind:
        per_kind = "  ".join(
            f"{k}={_fmt_pct(v)}" for k, v in sorted(m.approval_rate_by_kind.items())
        )
        click.echo(f"      by kind           {per_kind}")
    click.echo(f"    pending now         {m.pending_now}")
    click.echo("")
    click.echo("  corpus")
    click.echo(f"    claims              {m.claims_total}  ({m.claims_active} active)")
    click.echo(
        f"    citation coverage   {_fmt_pct(m.citation_coverage)}  "
        f"({m.claims_cited}/{m.claims_total} cited, "
        f"{m.citation_broken} broken)"
    )
    click.echo(
        f"    stale ratio         {_fmt_pct(m.stale_ratio)}  "
        f"({m.stale_claims} past {m.stale_after_days}d)"
    )
    if m.claims_by_status:
        hist = "  ".join(f"{k}={v}" for k, v in sorted(m.claims_by_status.items()))
        click.echo(f"    by status           {hist}")
    click.echo("")
    click.echo("  proposal lag (create → approve)")
    lag = m.proposal_lag
    click.echo(f"    samples             {lag.count}")
    click.echo(
        f"    p50 / p90 / p99     "
        f"{_fmt_secs(lag.p50)} / {_fmt_secs(lag.p90)} / {_fmt_secs(lag.p99)}"
    )
    click.echo(f"    mean / max          {_fmt_secs(lag.mean)} / {_fmt_secs(lag.max)}")
    if m.actors:
        click.echo("")
        click.echo("  actors (proposed / approved / rejected / confirmed)")
        for a in m.actors:
            click.echo(
                f"    {a.actor:<18} {a.proposed} / {a.approved} / {a.rejected} / {a.confirmed}"
            )
    click.echo("")
    click.echo(
        f"  audit: {m.audit_events_in_window} events in window ({m.audit_events_total} total)"
    )


# --- pages ------------------------------------------------------------------


@cli.command(name="pages")
@click.option("--kind", default=None, help="Filter by page kind (built-in or config-declared).")
@click.option(
    "--meta", "meta", multiple=True, metavar="K=V",
    help="Frontmatter equality filter (repeatable).",
)
@click.option(
    "--before", multiple=True, metavar="K=V",
    help="Inclusive upper bound on a frontmatter field (dates/numbers).",
)
@click.option(
    "--after", multiple=True, metavar="K=V",
    help="Inclusive lower bound on a frontmatter field (dates/numbers).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def pages_cmd(
    kind: str | None,
    meta: tuple[str, ...],
    before: tuple[str, ...],
    after: tuple[str, ...],
    as_json: bool,
) -> None:
    """List pages, optionally filtered by kind and frontmatter.

    Examples: `vouch pages --kind followup --meta followup_status=open
    --before due_at=2026-07-10` lists open followups due by july 10.
    """
    store = _load_store()
    try:
        equals, lo, hi = parse_kv(meta), parse_kv(after), parse_kv(before)
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    hits = filter_pages(
        store.list_pages(), kind=kind, equals=equals, before=hi, after=lo,
    )
    if as_json:
        _emit_json(
            [
                {
                    "id": p.id, "title": p.title, "type": p.type,
                    "tags": p.tags, "metadata": p.metadata,
                }
                for p in hits
            ]
        )
        return
    for p in hits:
        extras = " ".join(f"{k}={v}" for k, v in sorted(p.metadata.items()))
        suffix = f"  ({extras})" if extras else ""
        click.echo(f"{p.id}  [{p.type}]  {p.title}{suffix}")
    if not hits:
        click.echo("no matching pages")


# --- proposals ------------------------------------------------------------


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def pending(as_json: bool) -> None:
    """List proposals awaiting review."""
    store = _load_store()
    pending = store.list_proposals(ProposalStatus.PENDING)
    if as_json:
        _emit_json([pr.model_dump(mode="json") for pr in pending])
        return
    if not pending:
        click.echo("no pending proposals")
        return
    for pr in pending:
        preview = pr.payload.get("text") or pr.payload.get("title") or pr.payload.get("name") or "—"
        click.echo(f"• {pr.id}  [{pr.kind.value}]  by {pr.proposed_by}")
        click.echo(f"    {str(preview).strip()[:120]}")


def _proposal_preview(pr: Proposal) -> str:
    preview = (
        pr.payload.get("text")
        or pr.payload.get("title")
        or pr.payload.get("name")
        or pr.payload.get("id")
        or "-"
    )
    return str(preview).strip()


def _show_review_proposal(pr: Proposal, index: int, total: int) -> None:
    click.echo(f"\n[{index}/{total}] {pr.id}  [{pr.kind.value}]  by {pr.proposed_by}")
    click.echo(_proposal_preview(pr))
    if pr.rationale:
        click.echo(f"rationale: {pr.rationale}")
    click.echo()
    click.echo(yaml.safe_dump(pr.model_dump(mode="json"), sort_keys=False).rstrip())


@cli.command()
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Review at most N proposals.",
)
@click.option(
    "--type",
    "kind",
    type=click.Choice([k.value for k in ProposalKind]),
    default=None,
    help="Only review proposals of this kind.",
)
@click.option("--dry-run", is_flag=True, help="Show decisions without mutating proposals.")
def review(limit: int | None, kind: str | None, dry_run: bool) -> None:
    """Walk pending proposals one at a time for approval or rejection."""
    store = _load_store()
    proposals = store.list_proposals(ProposalStatus.PENDING)
    if kind is not None:
        proposals = [pr for pr in proposals if pr.kind.value == kind]
    if limit is not None:
        proposals = proposals[:limit]
    if not proposals:
        click.echo("no pending proposals")
        return

    decided = 0
    skipped = 0
    actor = _whoami()
    total = len(proposals)
    for index, pr in enumerate(proposals, start=1):
        _show_review_proposal(pr, index, total)
        action = click.prompt(
            "Action [a=approve, r=reject, s=skip, q=quit]",
            type=click.Choice(["a", "r", "s", "q"], case_sensitive=False),
            default="s",
            show_choices=False,
        ).lower()
        if action == "q":
            click.echo("Stopped review")
            break
        if action == "s":
            click.echo(f"Skipped {pr.id}")
            skipped += 1
            continue
        if action == "r":
            reason = click.prompt("Rejection reason").strip()
            with _cli_errors():
                if not reason:
                    raise ProposalError("rejection must include a reason (future agent context)")
                if not dry_run:
                    do_reject(store, pr.id, rejected_by=actor, reason=reason)
            if dry_run:
                click.echo(f"Would reject {pr.id}")
            else:
                click.echo(f"Rejected {pr.id}")
            decided += 1
            continue

        reason = click.prompt("Approval reason", default="", show_default=False).strip() or None
        if dry_run:
            click.echo(f"Would approve {pr.id}")
        else:
            with _cli_errors():
                artifact = do_approve(store, pr.id, approved_by=actor, reason=reason)
            click.echo(f"Approved -> {type(artifact).__name__.lower()}/{artifact.id}")
        decided += 1

    if dry_run:
        click.echo(f"Review complete: {decided} selected, {skipped} skipped, no changes made")
    else:
        click.echo(f"Review complete: {decided} decided, {skipped} skipped")


@cli.command()
@click.argument("proposal_id")
def show(proposal_id: str) -> None:
    """Show full details of a proposal."""
    store = _load_store()
    with _cli_errors():
        pr = store.get_proposal(proposal_id)
    click.echo(yaml.safe_dump(pr.model_dump(mode="json"), sort_keys=False))


@cli.command(name="read-claim")
@click.argument("claim_id")
def read_claim(claim_id: str) -> None:
    """Read an approved claim by id."""
    store = _load_store()
    with _cli_errors():
        claim = store.get_claim(claim_id)
    click.echo(yaml.safe_dump(claim.model_dump(mode="json"), sort_keys=False))


@cli.command(name="read-page")
@click.argument("page_id")
def read_page(page_id: str) -> None:
    """Read an approved page by id."""
    store = _load_store()
    with _cli_errors():
        page = store.get_page(page_id)
    click.echo(yaml.safe_dump(page.model_dump(mode="json"), sort_keys=False))


@cli.command(name="read-entity")
@click.argument("entity_id")
def read_entity(entity_id: str) -> None:
    """Read an approved entity by id."""
    store = _load_store()
    with _cli_errors():
        entity = store.get_entity(entity_id)
    click.echo(yaml.safe_dump(entity.model_dump(mode="json"), sort_keys=False))


@cli.command(name="read-relation")
@click.argument("relation_id")
def read_relation(relation_id: str) -> None:
    """Read an approved relation by id."""
    store = _load_store()
    with _cli_errors():
        relation = store.get_relation(relation_id)
    click.echo(yaml.safe_dump(relation.model_dump(mode="json"), sort_keys=False))


@cli.command(name="list-claims")
def list_claims() -> None:
    """List all approved claims."""
    store = _load_store()
    claims = store.list_claims()
    if not claims:
        click.echo("no claims found")
        return
    for claim in claims:
        click.echo(f"{claim.id:50} {claim.text}")


@cli.command(name="list-pages")
def list_pages() -> None:
    """List all approved pages."""
    store = _load_store()
    pages = store.list_pages()
    if not pages:
        click.echo("no pages found")
        return
    for page in pages:
        click.echo(f"{page.id:50} {page.title}")


@cli.command(name="list-entities")
def list_entities() -> None:
    """List all approved entities."""
    store = _load_store()
    entities = store.list_entities()
    if not entities:
        click.echo("no entities found")
        return
    for entity in entities:
        click.echo(f"{entity.id:50} {entity.name} ({entity.type})")


@cli.command(name="list-relations")
def list_relations() -> None:
    """List all approved relations."""
    store = _load_store()
    relations = store.list_relations()
    if not relations:
        click.echo("no relations found")
        return
    for relation in relations:
        output = f"{relation.id:50} {relation.source} -> {relation.relation} -> "
        output += relation.target
        click.echo(output)


@cli.command()
@click.argument("proposal_ids", nargs=-1)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable _meta.vouch_triage blocks.",
)
@click.option(
    "--reverse", is_flag=True,
    help="Ascending order (worst-first) instead of the default descending (best-first).",
)
def triage(proposal_ids: tuple[str, ...], as_json: bool, reverse: bool) -> None:
    """Advisory triage scoring over pending proposals (opt-in: triage.enabled).

    Scores each proposal on fit, citation quality, duplication risk, and
    contradiction risk, then prints a ranked table. Never approves or
    rejects — a human still decides via `vouch approve` / `vouch reject`.
    """
    from . import triage as triage_mod

    store = _load_store()
    with _cli_errors():
        results = triage_mod.triage_pending(store, proposal_ids=list(proposal_ids) or None)
    results.sort(key=lambda r: r["_meta"]["vouch_triage"]["score"], reverse=not reverse)

    if as_json:
        _emit_json(results)
        return
    if not results:
        click.echo("no pending proposals to triage")
        return
    for r in results:
        block = r["_meta"]["vouch_triage"]
        preview = (
            r["payload"].get("text")
            or r["payload"].get("title")
            or r["payload"].get("name")
            or r["payload"].get("id")
            or "-"
        )
        click.echo(
            f"{block['score']:.2f}  [{block['recommendation']:>11}]  "
            f"{r['id']}  [{r['kind']}]  {str(preview).strip()[:80]}"
        )
        click.echo(f"    {block['rationale']}")


@cli.command()
@click.argument("proposal_ids", nargs=-1, required=True)
@click.option("--reason", default=None)
@click.option(
    "--keep-going",
    is_flag=True,
    help="Best-effort: approve every id that can be approved and report the "
    "rest, instead of the default all-or-nothing precheck.",
)
def approve(proposal_ids: tuple[str, ...], reason: str | None, keep_going: bool) -> None:
    """Approve one or more proposals — converts each into a durable artifact.

    Pass several ids to approve a batch in one call (useful for CI and
    clearing a review backlog). One audit event is recorded per approved
    artifact.

    Semantics:

    \b
    - default (all-or-nothing): every id is validated as an approvable
      pending proposal before any is written; a typo or already-decided id
      aborts the whole batch and nothing is approved.
    - --keep-going (best-effort): approve each id independently, report the
      failures, and exit non-zero if any failed.
    """
    store = _load_store()
    approver = _whoami()

    if not keep_going:
        blocked = [
            (pid, reason_blocked)
            for pid in proposal_ids
            if (reason_blocked := check_approvable(store, pid, approved_by=approver))
        ]
        if blocked:
            for pid, why in blocked:
                click.echo(f"✗ {pid}: {why}", err=True)
            raise click.ClickException(
                f"refusing to approve: {len(blocked)} of {len(proposal_ids)} not "
                "approvable — nothing was approved (use --keep-going for best-effort)"
            )

    failures = 0
    for pid in proposal_ids:
        try:
            artifact = do_approve(store, pid, approved_by=approver, reason=reason)
        except (ArtifactNotFoundError, ValueError, ProposalError, LifecycleError) as e:
            failures += 1
            click.echo(f"✗ {pid}: {e}", err=True)
            continue
        click.echo(f"Approved → {type(artifact).__name__.lower()}/{artifact.id}")

    if failures:
        raise click.ClickException(
            f"{failures} of {len(proposal_ids)} proposal(s) failed to approve"
        )


@cli.command()
@click.argument("proposal_id")
@click.option("--reason", required=True)
def reject(proposal_id: str, reason: str) -> None:
    """Reject a proposal — recorded for audit and future agent context."""
    store = _load_store()
    with _cli_errors():
        do_reject(store, proposal_id, rejected_by=_whoami(), reason=reason)
    click.echo(f"Rejected {proposal_id}")


@cli.command("reject-extracted")
@click.option("--page", "page_id", default=None, help="Limit to edges extracted from one page id.")
@click.option("--reason", default="auto-extracted edge rejected in bulk")
def reject_extracted(page_id: str | None, reason: str) -> None:
    """Mass-reject pending edges the auto-extractor filed (issue #224)."""
    store = _load_store()
    with _cli_errors():
        rejected = reject_auto_extracted(
            store, rejected_by=_whoami(), page_id=page_id, reason=reason,
        )
    if not rejected:
        click.echo("no pending auto-extracted edges to reject")
        return
    click.echo(f"Rejected {len(rejected)} auto-extracted edge proposal(s)")


def _expire_row(proposal: Proposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "kind": proposal.kind.value,
        "proposed_by": proposal.proposed_by,
        "proposed_at": proposal.proposed_at.isoformat(),
    }


@cli.command()
@click.option("--apply", is_flag=True, help="Expire stale proposals (default is dry-run).")
@click.option(
    "--days", type=int, default=None, help="Override review.expire_pending_after_days for this run."
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def expire(apply: bool, days: int | None, as_json: bool) -> None:
    """Garbage-collect pending proposals older than the configured threshold."""
    store = _load_store()
    result = expire_pending(
        store,
        apply=apply,
        expired_by=EXPIRE_ACTOR,
        days=days,
    )
    if as_json:
        payload: dict[str, Any] = {
            "threshold_days": result.threshold_days,
            "enabled": result.threshold_days > 0,
            "dry_run": not apply,
            "would_expire": [_expire_row(p) for p in result.would_expire],
            "expired": [_expire_row(p) for p in result.expired],
        }
        _emit_json(payload)
        return

    if result.threshold_days <= 0:
        click.echo("expire disabled (review.expire_pending_after_days is 0)")
        return

    if not result.would_expire:
        click.echo(f"no stale pending proposals (threshold: {result.threshold_days} days)")
        return

    if not apply:
        click.echo(
            f"dry-run: {len(result.would_expire)} proposal(s) would expire "
            f"(threshold: {result.threshold_days} days)"
        )
        for pr in result.would_expire:
            click.echo(
                f"  {pr.id}  [{pr.kind.value}]  by {pr.proposed_by}  "
                f"proposed {pr.proposed_at.date().isoformat()}"
            )
        click.echo("rerun with --apply to expire")
        return

    click.echo(
        f"expired {len(result.expired)} proposal(s) (threshold: {result.threshold_days} days)"
    )
    for pr in result.expired:
        click.echo(f"  {pr.id}  [{pr.kind.value}]")


# --- proposal-from-CLI shortcuts -----------------------------------------


def _format_similarity_warning(w: dict) -> str:
    label = w.get("code", "similar")
    kind = w.get("artifact_kind", "?")
    aid = w.get("artifact_id", "?")
    cos = w.get("cosine", 0)
    snip = w.get("snippet", "")
    return f"warning: {label} {kind} {aid} (cosine {cos}) — {snip}"


@cli.command(name="propose-claim")
@click.option("--text", required=True)
@click.option(
    "--source", "sources", multiple=True, required=True, help="Source or evidence id. Repeatable."
)
@click.option("--type", "claim_type", default="observation", show_default=True)
@click.option("--confidence", default=0.7, show_default=True, type=float)
@click.option("--rationale", default=None)
@click.option("--tag", "tags", multiple=True)
def propose_claim_cmd(
    text: str,
    sources: tuple[str, ...],
    claim_type: str,
    confidence: float,
    rationale: str | None,
    tags: tuple[str, ...],
) -> None:
    store = _load_store()
    with _cli_errors():
        result = propose_claim(
            store,
            text=text,
            evidence=list(sources),
            proposed_by=_whoami(),
            claim_type=claim_type,
            confidence=confidence,
            tags=list(tags),
            rationale=rationale,
        )
    click.echo(result.id)
    for w in result.warnings:
        _echo(_format_similarity_warning(w), err=True)


@cli.command(name="propose-page")
@click.option("--title", required=True)
@click.option("--body", default="", help="Page body. Use `-` to read from stdin.")
@click.option("--type", "page_type", default="concept", show_default=True)
@click.option("--kind", "kind", default=None, help="alias for --type (config-declared page kind).")
@click.option(
    "--meta",
    "meta",
    multiple=True,
    help="per-kind frontmatter field as key=value (repeatable). Value parsed as YAML.",
)
@click.option("--claim", "claims", multiple=True)
@click.option("--entity", "entities", multiple=True)
def propose_page_cmd(
    title: str,
    body: str,
    page_type: str,
    kind: str | None,
    meta: tuple[str, ...],
    claims: tuple[str, ...],
    entities: tuple[str, ...],
) -> None:
    store = _load_store()
    if body == "-":
        body = sys.stdin.read()
    metadata = _parse_meta(meta)
    with _cli_errors():
        pr = propose_page(
            store,
            title=title,
            body=body,
            page_type=kind or page_type,
            claim_ids=list(claims),
            entity_ids=list(entities),
            metadata=metadata,
            proposed_by=_whoami(),
        )
    click.echo(pr.id)


def _parse_meta(pairs: tuple[str, ...], *, flag: str = "--meta") -> dict[str, Any]:
    """Parse repeated ``key=value`` pairs into a frontmatter dict.

    Values run through ``yaml.safe_load`` so ``attendees=[a, b]`` and
    ``count=3`` arrive as a list / int rather than strings.
    """
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"{flag} expects key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        key = key.strip()
        try:
            out[key] = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise click.BadParameter(
                f"{flag} value for {key!r} is invalid YAML: {e}",
            ) from e
    return out


# Keep entity scaffolding intentionally narrow because `propose_entity` accepts
# arbitrary type strings; only well-known EntityType names are routed here.
_SCAFFOLD_ENTITY_TYPES: frozenset[str] = frozenset(
    {"person", "project", "repo", "company", "concept", "decision", "workflow"}
)

_CITATION_REMINDER = (
    "\n\n<!-- citations required: attach at least one claim or source "
    "(--claim <id> / --source <id> on vouch new) -->\n"
)


def _field_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _resolve_new_kind(
    kind: str,
    registry: Any,
    *,
    force_entity: bool,
) -> tuple[Literal["page", "entity"], str]:
    if force_entity:
        if kind not in _SCAFFOLD_ENTITY_TYPES:
            known = ", ".join(sorted(_SCAFFOLD_ENTITY_TYPES))
            raise click.ClickException(f"unknown entity type {kind!r} (known: {known})")
        return "entity", kind
    if registry.is_known(kind):
        return "page", kind
    if kind in _SCAFFOLD_ENTITY_TYPES:
        return "entity", kind
    page_kinds = ", ".join(sorted(registry.known()))
    entity_kinds = ", ".join(sorted(_SCAFFOLD_ENTITY_TYPES))
    raise click.ClickException(
        f"unknown kind {kind!r}; page kinds: {page_kinds}; entity kinds: {entity_kinds}"
    )


def _stub_page_frontmatter(
    registry: Any,
    kind: str,
    prefilled: dict[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    required, _schema, required_citations = registry.resolve(kind)
    metadata = dict(prefilled)
    for field in required:
        metadata.setdefault(field, "")
    missing = [f for f in required if _field_missing(metadata.get(f))]
    return metadata, missing, required_citations


def _prompt_missing_fields(
    missing: list[str],
    metadata: dict[str, Any],
) -> list[str]:
    still_missing: list[str] = []
    for field in missing:
        raw = click.prompt(field, default="", show_default=False)
        if raw:
            try:
                metadata[field] = yaml.safe_load(raw)
            except yaml.YAMLError as e:
                raise click.BadParameter(
                    f"interactive value for {field!r} is invalid YAML: {e}",
                ) from e
        else:
            metadata[field] = ""
        if _field_missing(metadata.get(field)):
            still_missing.append(field)
    return still_missing


def _print_new_page_draft(draft: dict[str, Any]) -> None:
    click.echo(f"kind: {draft['kind']} (page)")
    click.echo(f"title: {draft['title']}")
    fm = yaml.safe_dump(draft["frontmatter"], default_flow_style=True).strip()
    click.echo(f"frontmatter: {fm}")
    missing = draft["missing_required_fields"]
    if missing:
        click.echo(f"missing required fields: {', '.join(missing)}")
    else:
        click.echo("missing required fields: (none)")
    if draft["citation_reminder"]:
        click.echo("citations: required (reminder appended to body)")
    if draft.get("body"):
        click.echo(f"body:\n{draft['body']}")
    if draft.get("id"):
        click.echo(f"proposal id (dry-run): {draft['id']}")


def _print_new_entity_draft(draft: dict[str, Any]) -> None:
    click.echo(f"kind: {draft['kind']} (entity)")
    click.echo(f"name: {draft['name']}")
    if draft.get("id"):
        click.echo(f"proposal id (dry-run): {draft['id']}")


@cli.command(name="new")
@click.argument("kind")
@click.option("--title", default=None, help="Page title (required for page kinds).")
@click.option("--name", default=None, help="Entity name (required for entity kinds).")
@click.option(
    "--field",
    "fields",
    multiple=True,
    help="Pre-fill a frontmatter field as key=value (repeatable). Value parsed as YAML.",
)
@click.option("--interactive", "-i", is_flag=True, help="Prompt for unfilled required fields.")
@click.option("--body", default="", help="Page body. Use `-` to read from stdin.")
@click.option("--claim", "claims", multiple=True)
@click.option("--source", "sources", multiple=True)
@click.option("--entity", "force_entity", is_flag=True, help="Force entity scaffold path.")
@click.option("--dry-run", is_flag=True, help="Print assembled draft without creating a proposal.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def new_cmd(
    kind: str,
    title: str | None,
    name: str | None,
    fields: tuple[str, ...],
    interactive: bool,
    body: str,
    claims: tuple[str, ...],
    sources: tuple[str, ...],
    force_entity: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Scaffold a typed page or entity proposal from the page-kind registry."""
    store = _load_store()
    registry = load_page_kind_registry(store)
    target, resolved_kind = _resolve_new_kind(kind, registry, force_entity=force_entity)

    if target == "entity":
        if not name or not name.strip():
            raise click.ClickException("--name is required for entity kinds")
        draft: dict[str, Any] = {
            "dry_run": dry_run,
            "target": "entity",
            "kind": resolved_kind,
            "name": name.strip(),
        }
        if dry_run:
            with _cli_errors():
                pr = propose_entity(
                    store,
                    name=name,
                    entity_type=resolved_kind,
                    proposed_by=_whoami(),
                    dry_run=True,
                )
            draft["id"] = pr.id
            if as_json:
                _emit_json(draft)
            else:
                _print_new_entity_draft(draft)
            return
        with _cli_errors():
            pr = propose_entity(
                store,
                name=name,
                entity_type=resolved_kind,
                proposed_by=_whoami(),
            )
        if as_json:
            _emit_json({"id": pr.id})
            return
        click.echo(pr.id)
        return

    if not title or not title.strip():
        raise click.ClickException("--title is required for page kinds")
    if body == "-":
        body = sys.stdin.read()

    metadata = _parse_meta(fields, flag="--field")
    metadata, missing, requires_citations = _stub_page_frontmatter(
        registry, resolved_kind, metadata,
    )
    if interactive and missing:
        missing = _prompt_missing_fields(missing, metadata)

    citation_reminder = requires_citations and not (claims or sources)
    if citation_reminder and not dry_run:
        raise click.ClickException(
            "this page kind requires citations; pass --claim/--source, "
            "or rerun with --dry-run to print a draft with the citation reminder"
        )
    if citation_reminder:
        body = body + _CITATION_REMINDER

    page_draft: dict[str, Any] = {
        "dry_run": dry_run,
        "target": "page",
        "kind": resolved_kind,
        "title": title.strip(),
        "frontmatter": metadata,
        "body": body,
        "missing_required_fields": missing,
        "citation_reminder": citation_reminder,
    }

    if dry_run:
        if not missing and not (requires_citations and not (claims or sources)):
            with _cli_errors():
                pr = propose_page(
                    store,
                    title=title,
                    body=body,
                    page_type=resolved_kind,
                    claim_ids=list(claims),
                    source_ids=list(sources),
                    metadata=metadata,
                    proposed_by=_whoami(),
                    dry_run=True,
                )
            page_draft["id"] = pr.id
        if as_json:
            _emit_json(page_draft)
        else:
            _print_new_page_draft(page_draft)
        return

    with _cli_errors():
        pr = propose_page(
            store,
            title=title,
            body=body,
            page_type=resolved_kind,
            claim_ids=list(claims),
            source_ids=list(sources),
            metadata=metadata,
            proposed_by=_whoami(),
        )
    if as_json:
        _emit_json({"id": pr.id})
        return
    click.echo(pr.id)


@cli.group(name="schema")
def schema() -> None:
    """inspect and validate config-declared page kinds (issue #234)."""


@schema.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="emit machine-readable JSON.")
def schema_list_cmd(as_json: bool) -> None:
    """list the page kinds this KB recognizes (built-in + config-declared)."""
    store = _load_store()
    registry = load_page_kind_registry(store)
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for name in sorted(registry.known()):
        required, fm_schema, citations = registry.resolve(name)
        rows.append(
            {
                "kind": name,
                "required_fields": required,
                "required_citations": citations,
                "has_frontmatter_schema": bool(fm_schema),
                "protected": registry.is_protected(name),
            }
        )
        extras: list[str] = []
        if required:
            extras.append(f"required={','.join(required)}")
        if citations:
            extras.append("citations-required")
        if fm_schema:
            extras.append("schema")
        if registry.is_protected(name):
            extras.append("protected")
        suffix = f"  ({'; '.join(extras)})" if extras else ""
        lines.append(f"{name}{suffix}")
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for line in lines:
        click.echo(line)


@schema.command(name="sync")
@click.option("--json", "as_json", is_flag=True, help="emit machine-readable JSON.")
def schema_sync_cmd(as_json: bool) -> None:
    """validate every page against its declared kind; report conflicts.

    Read-only: it never rewrites pages (that would bypass the review gate).
    Exits non-zero when any page conflicts, so it doubles as a CI guard after
    a `page_kinds` change. Resolve conflicts by re-proposing the page through
    the normal review flow.
    """
    store = _load_store()
    registry = load_page_kind_registry(store)
    conflicts: list[dict[str, Any]] = []
    checked = 0
    for page in store.list_pages():
        checked += 1
        try:
            registry.validate(
                page.type,
                page.metadata,
                has_citations=bool(page.claims or page.sources),
            )
        except PageKindError as e:
            conflicts.append({"page": page.id, "kind": page.type, "problems": e.problems})
    if as_json:
        click.echo(json.dumps({"checked": checked, "conflicts": conflicts}, indent=2))
    else:
        click.echo(f"checked {checked} page(s)")
        for c in conflicts:
            click.echo(f"  {c['page']} [{c['kind']}]: {'; '.join(c['problems'])}")
        if not conflicts:
            click.echo("no conflicts")
    if conflicts:
        raise SystemExit(1)


@cli.command(name="propose-entity")
@click.option("--name", required=True)
@click.option("--type", "entity_type", required=True)
@click.option("--alias", "aliases", multiple=True)
@click.option("--description", default=None)
def propose_entity_cmd(
    name: str, entity_type: str, aliases: tuple[str, ...], description: str | None
) -> None:
    store = _load_store()
    with _cli_errors():
        pr = propose_entity(
            store,
            name=name,
            entity_type=entity_type,
            aliases=list(aliases),
            description=description,
            proposed_by=_whoami(),
        )
    click.echo(pr.id)


@cli.command(name="propose-relation")
@click.option("--from", "src", required=True)
@click.option("--rel", "relation", required=True)
@click.option("--to", "target", required=True)
@click.option("--confidence", default=0.7, show_default=True, type=float)
def propose_relation_cmd(src: str, relation: str, target: str, confidence: float) -> None:
    store = _load_store()
    with _cli_errors():
        pr = propose_relation(
            store,
            src=src,
            relation=relation,
            target=target,
            confidence=confidence,
            proposed_by=_whoami(),
        )
    click.echo(pr.id)


# --- sources --------------------------------------------------------------


@cli.group()
def source() -> None:
    """Source management."""


@source.command("add")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--title", default=None)
@click.option("--url", default=None)
@click.option("--type", "source_type", default="file", show_default=True)
def source_add(path: str, title: str | None, url: str | None, source_type: str) -> None:
    """Register a file as a Source; prints its sha256 id."""
    store = _load_store()
    data = Path(path).read_bytes()
    with _cli_errors():
        src = store.put_source(
            data,
            title=title or Path(path).name,
            url=url,
            locator=str(Path(path).resolve()),
            source_type=source_type,
        )
    audit_mod.log_event(
        store.kb_dir,
        event="source.add",
        actor=_whoami(),
        object_ids=[src.id],
    )
    click.echo(src.id)


@source.command("fetch")
@click.argument("url")
@click.option("--title", default=None)
@click.option(
    "--max-bytes",
    default=fetch_mod.DEFAULT_MAX_BYTES,
    show_default=True,
    type=int,
    help="Snapshot size cap.",
)
@click.option("--timeout", default=fetch_mod.DEFAULT_TIMEOUT, show_default=True, type=float)
@click.option("--tag", "tags", multiple=True)
def source_fetch(
    url: str, title: str | None, max_bytes: int, timeout: float, tags: tuple[str, ...],
) -> None:
    """Fetch URL and register the exact bytes as a content-addressed Source.

    Claims cite the immutable snapshot id, so the evidence a reviewer
    approved against survives the live page drifting. http/https only;
    hosts must resolve to public addresses; redirects are re-validated.
    """
    store = _load_store()
    with _cli_errors():
        src = fetch_mod.snapshot_url(
            store,
            url,
            title=title,
            tags=list(tags) or None,
            max_bytes=max_bytes,
            timeout=timeout,
        )
    audit_mod.log_event(
        store.kb_dir,
        event="source.fetch",
        actor=_whoami(),
        object_ids=[src.id],
        data={"url": url},
    )
    click.echo(src.id)


@source.command("verify")
@click.option("--fail-on-issue", is_flag=True)
def source_verify(fail_on_issue: bool) -> None:
    """Re-hash every source and report drift."""
    store = _load_store()
    bad = 0
    for vr in verify_mod.verify_all(store):
        marker = "ok" if (vr.stored_ok and vr.external_status in {"match", "n/a"}) else "!"
        if marker == "!":
            bad += 1
        click.echo(
            f"{marker}  {vr.source.id[:12]}  stored={'ok' if vr.stored_ok else 'BAD'}  "
            f"external={vr.external_status}  {vr.source.locator}"
        )
    if fail_on_issue and bad:
        sys.exit(1)


@cli.command(name="inbox")
@click.option(
    "--dir", "directory", required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Folder to scan (must live under the project root).",
)
@click.option("--watch", "watch_mode", is_flag=True, help="Poll instead of a single pass.")
@click.option(
    "--poll-interval",
    default=inbox_mod.DEFAULT_POLL_INTERVAL,
    show_default=True,
    type=float,
)
@click.option("--once", is_flag=True, help="Single tick even under --watch (test/ci bound).")
def inbox_cmd(directory: str, watch_mode: bool, poll_interval: float, once: bool) -> None:
    """Scan an inbox folder: each new file becomes a registered source plus
    one pending page proposal. Proposes only — a human still approves."""
    store = _load_store()
    path = Path(directory)
    with _cli_errors():
        if watch_mode and not once:
            def _report(res: inbox_mod.ScanResult) -> None:
                if res.proposed:
                    click.echo(f"filed {len(res.proposed)} proposal(s): {', '.join(res.proposed)}")

            with suppress(KeyboardInterrupt):
                inbox_mod.watch(
                    store, path, poll_interval=poll_interval, on_result=_report,
                )
            return
        res = inbox_mod.scan(store, path)
    click.echo(f"filed {len(res.proposed)} proposal(s); skipped {len(res.skipped)} file(s)")
    for pid in res.proposed:
        click.echo(f"  {pid}")


@cli.group()
def notify() -> None:
    """Outbound reviewer notification webhooks (config: notify.webhooks)."""


@notify.command("sweep")
def notify_sweep() -> None:
    """Evaluate pending-queue triggers and fire configured webhooks.

    Idempotent per (event, proposal) — safe to run from cron. Read-and-
    notify only: nothing here can propose, approve, or edit."""
    store = _load_store()
    with _cli_errors():
        fired = notify_mod.sweep(store)
    if fired:
        click.echo(f"fired {len(fired)} event(s): {', '.join(fired)}")
    else:
        click.echo("nothing to fire")


@notify.command("test")
@click.option("--url", required=True)
@click.option("--secret", default=None, help="Optional hmac secret (or env:VAR).")
def notify_test(url: str, secret: str | None) -> None:
    """Send a synthetic event to URL and report delivery."""
    resolved = None
    if secret:
        with _cli_errors():
            resolved = notify_mod._resolve_env(secret, what="--secret")
    ok = notify_mod.send_test(url, secret=resolved)
    click.echo("delivered" if ok else "delivery failed")
    if not ok:
        sys.exit(1)


# --- lifecycle ------------------------------------------------------------


@cli.command()
@click.argument("old_claim_id")
@click.argument("new_claim_id")
def supersede(old_claim_id: str, new_claim_id: str) -> None:
    """Mark OLD as superseded by NEW."""
    store = _load_store()
    with _cli_errors():
        life.supersede(store, old_claim_id=old_claim_id, new_claim_id=new_claim_id, actor=_whoami())
    click.echo(f"superseded {old_claim_id} -> {new_claim_id}")


@cli.command()
@click.argument("claim_a")
@click.argument("claim_b")
def contradict(claim_a: str, claim_b: str) -> None:
    """Record that two claims contradict each other."""
    store = _load_store()
    with _cli_errors():
        life.contradict(store, claim_a=claim_a, claim_b=claim_b, actor=_whoami())
    click.echo(f"contradiction recorded: {claim_a} <-> {claim_b}")


@cli.command()
@click.argument("claim_id")
def archive(claim_id: str) -> None:
    """Archive a claim (kept for history, omitted from default retrieval)."""
    store = _load_store()
    with _cli_errors():
        life.archive(store, claim_id=claim_id, actor=_whoami())
    click.echo(f"archived {claim_id}")


@cli.command(name="claims-clear")
@click.option("--auto-only", is_flag=True, default=True, show_default=True,
              help="Clear only auto-approved claims (default: yes)")
@click.option("--before", type=str, default=None,
              help="Clear only claims created before this date (ISO 8601, e.g. 2026-07-01)")
@click.option("--confirm", is_flag=True, default=False,
              help="Skip confirmation prompt")
@click.option("--dry-run", is_flag=True, default=False,
              help="Preview what would be cleared without making changes")
def claims_clear(auto_only: bool, before: str | None, confirm: bool, dry_run: bool) -> None:
    """Clear auto-saved claims. Archived claims are preserved in history."""
    from datetime import datetime

    store = _load_store()
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError as err:
            raise click.ClickException(
                f"invalid date format: {before} (use ISO 8601, e.g. 2026-07-01)"
            ) from err

    with _cli_errors():
        to_clear = life.clear_claims(
            store,
            auto_only=auto_only,
            before=before_dt,
            actor=_whoami(),
            dry_run=True,  # Always dry-run first to show what will be cleared
        )

    if not to_clear:
        click.echo("no claims match the criteria")
        return

    click.echo(f"found {len(to_clear)} claims to clear:")
    for claim in to_clear[:10]:  # Show first 10
        click.echo(f"  {claim.id}: {claim.text[:60]}")
    if len(to_clear) > 10:
        click.echo(f"  ... and {len(to_clear) - 10} more")

    if dry_run:
        click.echo("(dry-run mode: no changes made)")
        return

    if not confirm and not click.confirm(f"\nClear {len(to_clear)} claims?"):
        click.echo("cancelled")
        return

    # Now actually clear them
    with _cli_errors():
        life.clear_claims(
            store,
            auto_only=auto_only,
            before=before_dt,
            actor=_whoami(),
            dry_run=False,
        )

    click.echo(f"cleared {len(to_clear)} claims")


@cli.command()
@click.argument("claim_id")
def confirm(claim_id: str) -> None:
    """Re-confirm a claim — bumps last_confirmed_at."""
    store = _load_store()
    with _cli_errors():
        life.confirm(store, claim_id=claim_id, actor=_whoami())
    click.echo(f"confirmed {claim_id}")


@cli.command()
@click.argument("claim_id")
def cite(claim_id: str) -> None:
    """Resolve and print all citations backing a claim."""
    store = _load_store()
    out = []
    with _cli_errors():
        for c in life.cite(store, claim_id):
            out.append(c.model_dump(mode="json") if hasattr(c, "model_dump") else c)
    _emit_json(out)


# --- sessions -------------------------------------------------------------


@cli.group()
def session() -> None:
    """Agent session lifecycle."""


@session.command("start")
@click.option(
    "--agent",
    default=None,
    help="Agent id (defaults to $VOUCH_AGENT or current user).",
)
@click.option("--task", default=None)
@click.option("--note", default=None)
def session_start_cmd(agent: str | None, task: str | None, note: str | None) -> None:
    store = _load_store()
    sess = sess_mod.session_start(
        store,
        agent=agent or os.environ.get("VOUCH_AGENT") or _whoami(),
        task=task,
        note=note,
    )
    click.echo(sess.id)


@session.command("volunteer")
@click.argument("session_id")
@click.option("--no-clear", is_flag=True, help="Peek without draining the queue.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def session_volunteer_cmd(session_id: str, no_clear: bool, as_json: bool) -> None:
    """Poll volunteered context for an active session."""
    offers = volunteer_context.drain_pending(session_id, clear=not no_clear)
    payload = {"volunteers": [o.to_dict() for o in offers]}
    if as_json:
        _emit_json(payload)
        return
    if not offers:
        click.echo("(no volunteered context)")
        return
    for offer in offers:
        click.echo(
            f"{offer.claim_id}  relevance={offer.relevance:.2f}  {offer.why}"
        )


@session.command("end")
@click.argument("session_id")
@click.option("--note", default=None)
def session_end_cmd(session_id: str, note: str | None) -> None:
    store = _load_store()
    with _cli_errors():
        sess = sess_mod.session_end(store, session_id, note=note)
    _emit_json({"session": sess.id, "proposals": sess.proposal_ids})


@cli.group()
def capture() -> None:
    """Automatic session capture (driven by claude code hooks)."""


def _capture_store() -> KBStore | None:
    """Locate the KB without the sys.exit(2) that _load_store does — hooks
    must never abort the host."""
    try:
        return KBStore(discover_root())
    except KBNotFoundError:
        return None


@capture.command("observe")
def capture_observe_cmd() -> None:
    """Append one observation from a PostToolUse hook payload (stdin JSON)."""
    if sys.stdin.isatty():
        return
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return
        tool_input = payload.get("tool_input")
        obs = capture_mod.summarize_tool(
            payload.get("tool_name"),
            tool_input if isinstance(tool_input, dict) else {},
            payload.get("tool_response"),
        )
        if obs is None:
            return
        store = _capture_store()
        if store is None:
            return
        capture_mod.observe(
            store, session_id,
            tool=obs["tool"], summary=obs["summary"],
            files=obs.get("files"), cmd=obs.get("cmd"),
        )
    except Exception:
        # a capture failure must never break the user's tool call.
        return


@capture.command("finalize")
@click.option("--session-id", default=None, help="Session id (else read from stdin payload).")
def capture_finalize_cmd(session_id: str | None) -> None:
    """Roll a session buffer into a PENDING summary (SessionEnd hook payload on stdin)."""
    payload: dict[str, Any] = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    payload = loaded
            except json.JSONDecodeError:
                payload = {}
    sid = session_id or str(payload.get("session_id") or "")
    if not sid:
        return
    store = _capture_store()
    if store is None:
        return
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    transcript_raw = payload.get("transcript_path")
    transcript = Path(str(transcript_raw)) if transcript_raw else None
    result = capture_mod.finalize(
        store, sid, cwd=cwd, project=cwd.name,
        generated_at=datetime.now(UTC).isoformat(),
        transcript_path=transcript,
    )
    _emit_json(result)


@capture.command("finalize-all")
@click.option("--session-id", default=None, help="Current session id (else env VOUCH_SESSION_ID).")
@click.option("--max-age-seconds", type=float, default=3600.0, help="Max age in seconds.")
def capture_finalize_all_cmd(session_id: str | None, max_age_seconds: float) -> None:
    """Finalize all capture buffers except current session (SessionStart cleanup)."""
    sid = session_id or os.environ.get("VOUCH_SESSION_ID") or ""
    if not sid:
        # No session ID provided; silently succeed
        _emit_json({"finalized": [], "skipped_recent": [], "skipped_current": []})
        return

    store = _capture_store()
    if store is None:
        # No KB; silently succeed
        _emit_json({"finalized": [], "skipped_recent": [], "skipped_current": []})
        return

    result = capture_mod.finalize_all_except(
        store, sid, max_age_seconds=max_age_seconds,
    )
    _emit_json(result)


@capture.command("ingest-codex")
@click.argument(
    "rollout", required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--latest", is_flag=True,
    help="Resolve the newest codex rollout recorded for this project (by cwd).",
)
@click.option(
    "--hook", "hook_mode", is_flag=True,
    help="Read a codex Stop-hook payload from stdin; never fails the host "
         "(exits 0 even on errors, like `capture observe`).",
)
@click.option(
    "--codex-home", type=click.Path(file_okay=False, path_type=Path), default=None,
    help="Codex state dir holding sessions/ (default: $CODEX_HOME or ~/.codex).",
)
def capture_ingest_codex_cmd(
    rollout: Path | None, latest: bool, hook_mode: bool, codex_home: Path | None
) -> None:
    """Ingest one codex session rollout into a PENDING summary proposal.

    Codex has no live notify stream vouch can use project-locally; it
    persists each session as a rollout jsonl instead. This maps the
    rollout's tool calls into the same observation shape `capture observe`
    produces and reuses the existing rollup, so the result is the same
    review-gated summary a claude session yields. Re-ingesting an
    unchanged session is a no-op; a session that grew since the last
    ingest refreshes its one PENDING proposal in place. Review with
    `vouch review`.
    """
    if hook_mode:
        # Hook wire (codex `Stop` event): parse the stdin payload, resolve
        # the session's rollout, ingest idempotently. Exits 0 no matter
        # what — capture must never break the user's codex turn.
        try:
            raw = "" if sys.stdin.isatty() else sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if isinstance(payload, dict):
                codex_rollout_mod.ingest_hook_payload(
                    _capture_store(), payload, codex_home=codex_home
                )
        except Exception:
            # the hook contract is exit 0 — never surface an error here.
            pass
        return
    if (rollout is None) == (not latest):
        raise click.ClickException("pass exactly one of ROLLOUT or --latest")
    store = _load_store()
    with _cli_errors():
        if latest:
            found = codex_rollout_mod.find_latest_rollout(
                Path.cwd(), codex_home=codex_home
            )
            if found is None:
                raise click.ClickException(
                    "no codex rollout found for this project under "
                    f"{(codex_home or codex_rollout_mod.default_codex_home()) / 'sessions'}; "
                    "pass a rollout file explicitly"
                )
            rollout = found
        assert rollout is not None
        result = codex_rollout_mod.ingest_rollout(
            store, rollout, generated_at=datetime.now(UTC).isoformat()
        )
    _emit_json(result)


@capture.command("banner")
def capture_banner_cmd() -> None:
    """Emit a SessionStart nudge if captured summaries await review."""
    store = _capture_store()
    if store is None:
        return
    n = capture_mod.pending_count(store)
    if n:
        click.echo(
            f"🔔 {n} auto-captured session summary(ies) awaiting review — "
            f"run `vouch review`."
        )


@cli.command(name="recall")
def recall_cmd() -> None:
    """Emit a digest of all approved knowledge for session-start injection."""
    store = _capture_store()
    if store is None:
        return
    cfg = recall_mod.load_config(store)
    if not cfg.enabled:
        return
    digest = recall_mod.build_digest(store, max_chars=cfg.max_chars)
    if digest.strip():
        click.echo(digest)


@cli.command(name="compile")
@click.option("--dry-run", is_flag=True, help="Draft and validate; file nothing.")
@click.option("--max-pages", type=int, default=None,
              help="Cap drafted pages (default: compile.max_pages, 5).")
@click.option("--llm-cmd", default=None,
              help="Override compile.llm_cmd from config.yaml for this run.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report.")
def compile_cmd(dry_run: bool, max_pages: int | None,
                llm_cmd: str | None, as_json: bool) -> None:
    """Compile approved claims into topic-page proposals (llm-wiki ingest).

    Runs the deployment-configured LLM (compile.llm_cmd) over the live
    approved claims, validates every citation in the drafts, and files the
    survivors as pending page proposals. Approval stays a separate human
    step (`vouch review`).
    """
    store = _load_store()
    actor = os.environ.get("VOUCH_AGENT") or compile_mod.COMPILE_ACTOR
    try:
        report = compile_mod.compile_kb(
            store, actor=actor, triggered_by=_whoami(), llm_cmd=llm_cmd,
            max_pages=max_pages, dry_run=dry_run,
        )
    except compile_mod.CompileError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        _emit_json(report.to_dict())
        return
    verb = "would propose" if dry_run else "proposed"
    _echo(f"{verb} {len(report.proposed)} page draft(s):")
    for row in report.proposed:
        _echo(f"  • {row['proposal_id']}  {row['title']}")
    if report.dropped:
        _echo(f"dropped {len(report.dropped)}:")
        for row in report.dropped:
            _echo(f"  • {row['title']} — {row['reason']}")
    if report.proposed and not dry_run:
        _echo("run `vouch review` to decide.")


@cli.command()
@click.argument("session_id")
@click.option("--no-page", is_flag=True, help="Skip the session-summary page.")
def crystallize(session_id: str, no_page: bool) -> None:
    """Approve every pending proposal in a session (and write a summary page)."""
    store = _load_store()
    with _cli_errors():
        result = sess_mod.crystallize(
            store,
            session_id,
            approver=_whoami(),
            write_summary_page=not no_page,
        )
    _emit_json(result)
    n_approved = len(result["approved"])
    n_failed = len(result["failures"])
    total = n_approved + n_failed
    if total > 0 and n_failed == total:
        click.echo(
            f"error: all {total} proposal(s) failed to approve — crystallize aborted",
            err=True,
        )
        raise SystemExit(1)
    if n_failed > 0:
        click.echo(
            f"warning: {n_failed}/{total} proposal(s) failed to approve "
            f"(see failures in JSON above)",
            err=True,
        )


# --- retrieval ------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("--limit", "-n", default=10, show_default=True, type=int)
@click.option("--top-k", default=None, type=int, help="Alias for --limit.")
@click.option(
    "--semantic/--no-semantic",
    default=None,
    help="Force semantic backend (alias for --backend embedding).",
)
@click.option(
    "--backend",
    type=click.Choice(["auto", "embedding", "fts5", "substring", "hybrid"]),
    default="auto",
    show_default=True,
)
@click.option("--min-score", default=0.0, show_default=True, type=float)
@click.option("--rerank/--no-rerank", default=False)
@click.option("--hyde/--no-hyde", default=False)
@click.option("--explain/--no-explain", default=False)
@click.option("--json", "as_json", is_flag=True, help="Emit hits as JSON.")
@click.option("--project", default=None, help="Viewer project for scope filtering.")
@click.option("--agent", default=None, help="Viewer agent for scope filtering.")
def search(
    query: str,
    limit: int,
    top_k: int | None,
    semantic: bool | None,
    backend: str,
    min_score: float,
    rerank: bool,
    hyde: bool,
    explain: bool,
    as_json: bool,
    project: str | None,
    agent: str | None,
) -> None:
    """Search the KB."""
    from . import index_db
    from .embeddings.fusion import rrf_fuse
    from .scoping import filter_hits, scoped_fetch_limit, viewer_from

    store = _load_store()
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    fetch_limit = scoped_fetch_limit(limit, viewer)
    if top_k is not None:
        limit = top_k
    if semantic is True:
        backend = "embedding"
    elif semantic is False:
        backend = "fts5"
    q = query
    if hyde:
        from .embeddings.hyde import expand_query_template

        q = expand_query_template(query)

    hits: list[tuple[str, str, str, float]] = []
    used = backend
    if backend in ("auto", "embedding"):
        hits = index_db.search_semantic(
            store.kb_dir,
            q,
            limit=fetch_limit,
            min_score=min_score,
        )
        used = "embedding" if hits else used
    if not hits and backend in ("auto", "fts5"):
        hits = index_db.search(store.kb_dir, q, limit=fetch_limit)
        used = "fts5" if hits else used
    if not hits and backend in ("auto", "substring"):
        hits = store.search_substring(q, limit=fetch_limit)
        used = "substring"
    if backend == "hybrid":
        emb = index_db.search_semantic(store.kb_dir, q, limit=fetch_limit * 2)
        fts = index_db.search(store.kb_dir, q, limit=fetch_limit * 2)
        hits = rrf_fuse(emb, fts, limit=fetch_limit)
        used = "hybrid"

    hits = filter_hits(store, hits, viewer, limit=limit)

    if rerank and hits:
        try:
            from .embeddings.rerank import default_reranker
            from .embeddings.rerank import rerank as do_rerank

            hits = do_rerank(query=query, hits=hits, reranker=default_reranker(), top_k=limit)
        except ImportError:
            click.echo("warning: rerank extras not installed; skipping rerank", err=True)

    if as_json:
        _emit_json({
            "backend": used,
            "viewer": {"project": viewer.project, "agent": viewer.agent},
            "hits": [
                {"kind": k, "id": i, "snippet": snip, "score": score,
                 "backend": used}
                for k, i, snip, score in hits
            ],
        })
        return

    for k, i, snip, score in hits:
        if explain:
            click.echo(f"[{used}] {k}/{i}\tscore={score:.4f}\t{snip}  ({used})")
        else:
            click.echo(f"{k}/{i}\t{snip}  ({used})")


@cli.command()
@click.argument("node_id")
@click.option("--depth", default=1, show_default=True, type=int)
@click.option("--rel-type", "rel_types", multiple=True,
              help="Filter to relation types (repeatable).")
@click.option("--max-nodes", default=50, show_default=True, type=int)
def neighbors(node_id: str, depth: int, rel_types: tuple[str, ...],
              max_nodes: int) -> None:
    """List graph neighbors of a claim, page, entity, or source."""
    from .graph import find_neighbors

    store = _load_store()
    with _cli_errors():
        result = find_neighbors(
            store, node_id, depth=depth,
            rel_types=list(rel_types) or None,
            max_nodes=max_nodes,
        )
    _emit_json(result)


@cli.command()
@click.argument("task")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--max-chars", default=None, type=int)
@click.option("--require-citations", is_flag=True)
@click.option("--min-items", default=0, type=int)
@click.option("--project", default=None, help="Viewer project for scope filtering.")
@click.option("--agent", default=None, help="Viewer agent for scope filtering.")
@click.option("--expand-graph", is_flag=True,
              help="Include 1-hop graph neighbors of search hits.")
@click.option("--graph-depth", default=1, show_default=True, type=int)
@click.option("--graph-limit", default=20, show_default=True, type=int)
def context(
    task: str,
    limit: int,
    max_chars: int | None,
    require_citations: bool,
    min_items: int,
    project: str | None,
    agent: str | None,
    expand_graph: bool,
    graph_depth: int,
    graph_limit: int,
) -> None:
    """Build a ContextPack ready to inject into an agent prompt."""
    store = _load_store()
    pack = build_context_pack(
        store,
        query=task,
        limit=limit,
        max_chars=max_chars,
        min_items=min_items,
        require_citations=require_citations,
        project=project,
        agent=agent,
        expand_graph=expand_graph,
        graph_depth=graph_depth,
        graph_limit=graph_limit,
    )
    _emit_json(pack)


@cli.command(name="context-hook", hidden=True)
def context_hook() -> None:
    """Emit relevant KB context for a host UserPromptSubmit hook (reads stdin).

    Wired by the claude-code adapter; not meant to be run by hand. Reads the
    host's JSON hook payload on stdin, prints an additionalContext envelope,
    and always exits 0 so it can never block a turn.
    """
    import sys

    from . import hooks

    stdin_text = sys.stdin.read()
    store = _capture_store()
    out = ""
    if store is not None:
        try:
            out = hooks.build_claude_prompt_hook(store, stdin_text)
        except Exception:
            out = ""
    if out:
        click.echo(out)


@cli.command()
@click.argument("query")
@click.option("--depth", default=3, show_default=True, type=int)
@click.option("--max-chars", default=4000, show_default=True, type=int)
def synthesize(query: str, depth: int, max_chars: int) -> None:
    """Answer a query from approved claims only, with inline citations."""
    store = _load_store()
    with _cli_errors():
        result = synth.synthesize(
            store, query=query, depth=depth, max_chars=max_chars,
        )
    _emit_json(result)


@cli.command()
def index() -> None:
    """Rebuild state.db from durable files."""
    store = _load_store()
    stats = health.rebuild_index(store)
    click.echo(f"indexed: {stats}")


# --- provenance: why / trace / impact / graph -----------------------------


@cli.command()
@click.argument("claim_id")
@click.option(
    "--depth", default=3, show_default=True, type=int, help="How many hops of provenance to expand."
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a tree.")
def why(claim_id: str, depth: int, as_json: bool) -> None:
    """Explain why a claim exists: cites, session, supersedes chain, approval.

    \b
    Examples:
      vouch why my-claim-id
      vouch why my-claim-id --depth 5 --json
    """
    store = _load_store()
    with _cli_errors():
        result = prov_mod.why(store, claim_id=claim_id, depth=depth)
    if as_json:
        _emit_json(result)
        return
    _echo(prov_mod.render_why(result))


@cli.command()
@click.argument("from_id")
@click.option("--to", "to_id", required=True, help="The artifact to trace a path to.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def trace(from_id: str, to_id: str, as_json: bool) -> None:
    """Find the shortest typed-edge path between two artifacts.

    Exits non-zero with `no path` when the two artifacts are disconnected.
    """
    store = _load_store()
    with _cli_errors():
        result = prov_mod.trace(store, from_id=from_id, to_id=to_id)
    if as_json:
        _emit_json(result)
    else:
        _echo(prov_mod.render_trace(result))
    if not result["found"]:
        sys.exit(1)


@cli.command()
@click.argument("claim_id")
@click.option(
    "--depth", default=1, show_default=True, type=int, help="How many hops of dependents to expand."
)
@click.option(
    "--if",
    "if_op",
    default=None,
    type=click.Choice([op.value for op in prov_mod.LifecycleOp]),
    help="Dry-run a lifecycle op and report breakage (exit non-zero if any).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a tree.")
def impact(claim_id: str, depth: int, if_op: str | None, as_json: bool) -> None:
    """Show what depends on a claim, and what breaks if you change it.

    \b
    Examples:
      vouch impact my-claim-id
      vouch impact my-claim-id --if archive
    """
    store = _load_store()
    with _cli_errors():
        result = prov_mod.impact(store, claim_id=claim_id, depth=depth, op=if_op)
    if as_json:
        _emit_json(result)
    else:
        _echo(prov_mod.render_impact(result))
    if if_op is not None and result["blocking"]:
        sys.exit(1)


@cli.command()
@click.option("--session", default=None, help="Restrict to one agent run's subgraph.")
@click.option(
    "--format",
    "fmt",
    default="dot",
    show_default=True,
    type=click.Choice(["dot", "mermaid"]),
    help="Output format for the DAG.",
)
def graph(session: str | None, fmt: str) -> None:
    """Render the provenance DAG as Graphviz dot or a mermaid flowchart."""
    store = _load_store()
    with _cli_errors():
        text = prov_mod.graph_export(store, session=session, fmt=fmt)
    click.echo(text, nl=False)


@cli.group()
def provenance() -> None:
    """Provenance graph cache operations."""


@provenance.command("rebuild")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def provenance_rebuild(as_json: bool) -> None:
    """Rebuild the prov_edges cache from durable files (treated as derived state)."""
    store = _load_store()
    with _cli_errors():
        count = prov_mod.rebuild_prov_edges(store)
    if as_json:
        _emit_json({"edges": count})
        return
    click.echo(f"provenance: rebuilt {count} edges")


@cli.command()
@click.option("--threshold", default=0.95, show_default=True, type=float)
@click.option("--dry-run/--no-dry-run", default=False)
def dedup(threshold: float, dry_run: bool) -> None:
    """Scan embeddings for cross-artifact near-duplicates."""
    from .embeddings.dedup import scan_all

    store = _load_store()
    rows = scan_all(store.kb_dir, threshold=threshold, dry_run=dry_run)
    if not rows:
        click.echo("dedup: no duplicates found")
        return
    for r in rows:
        click.echo(f"{r['kind']}/{r['id']} ~ {r['kind']}/{r['near_id']}  cos={r['cosine']:.4f}")


@cli.group()
def embeddings() -> None:
    """Embedding maintenance commands."""


@embeddings.command("stats")
def embeddings_stats() -> None:
    """Print model identity, per-kind counts, and cache hit rate."""
    from . import index_db
    from .embeddings.cache import query_cache_stats

    store = _load_store()
    meta = index_db.get_embedding_meta(store.kb_dir)
    for k, v in sorted(meta.items()):
        click.echo(f"{k}\t{v}")
    with index_db.open_db(store.kb_dir) as conn:
        rows = conn.execute("SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind").fetchall()
    for k, n in rows:
        click.echo(f"embedding_count_{k}\t{n}")
    cs = query_cache_stats(store.kb_dir)
    click.echo(f"query_cache_entries\t{cs['entries']}")
    click.echo(f"query_cache_hits\t{cs['hits']}")


@cli.group(name="eval")
def eval_group() -> None:
    """Evaluation harnesses."""


@eval_group.command("embedding")
@click.option("--queries", required=True, type=click.Path(exists=True))
@click.option("--metric", default="recall@10,mrr,ndcg")
def eval_embedding(queries: str, metric: str) -> None:
    """Run retrieval-quality metrics over a JSONL query set."""
    from pathlib import Path as _Path

    from .embeddings.scorer import evaluate

    store = _load_store()
    metrics = tuple(m.strip() for m in metric.split(","))
    canonical = tuple("recall@k" if m.startswith("recall@") else m for m in metrics)
    import contextlib

    k = 10
    for m in metrics:
        if m.startswith("recall@"):
            with contextlib.suppress(ValueError):
                k = int(m.split("@", 1)[1])
    out = evaluate(
        kb_dir=store.kb_dir,
        queries_file=_Path(queries),
        k=k,
        metrics=canonical,
    )
    for m_name, v in out.items():
        click.echo(f"{m_name}\t{v:.4f}")


@eval_group.command("recall")
@click.argument("queries", type=click.Path(exists=True, dir_okay=False))
@click.option("--k", default=5, show_default=True, type=int)
@click.option("--baseline", default=None, type=click.Path(exists=True, dir_okay=False),
              help="Baseline report JSON; fail on a P@k regression beyond tolerance.")
@click.option("--max-regression", default=0.05, show_default=True, type=float)
def eval_recall(queries: str, k: int, baseline: str | None,
                max_regression: float) -> None:
    """Score kb.context retrieval against a labeled query set (P@k/R@k/MRR/nDCG)."""
    from .eval.recall import compare_baseline, run_recall
    store = _load_store()
    with _cli_errors():
        report = run_recall(store, queries, k=k)
    click.echo(json.dumps(report, indent=2))
    if baseline is not None:
        base = json.loads(Path(baseline).read_text(encoding="utf-8"))
        ok, message = compare_baseline(report, base, max_regression=max_regression)
        click.echo(message, err=True)
        if not ok:
            raise click.ClickException(message)


@cli.command()
@click.option(
    "--embeddings/--no-embeddings",
    default=False,
    help="Rebuild the embedding index in addition to FTS5.",
)
@click.option(
    "--backfill/--no-backfill",
    default=False,
    help="Re-encode every artifact under the current model.",
)
@click.option("--force/--no-force", default=False, help="Re-encode even if content hash unchanged.")
@click.option("--model", default=None, help="Adapter name; defaults to the registered default.")
def reindex(embeddings: bool, backfill: bool, force: bool, model: str | None) -> None:
    """Rebuild derived indexes from on-disk artifacts."""
    store = _load_store()
    health.rebuild_index(store)
    if embeddings or backfill:
        from .embeddings.migration import backfill_embeddings

        if model:
            from .embeddings import get_embedder

            get_embedder(model)
        n = backfill_embeddings(store, force=force)
        click.echo(f"reindex: embeddings backfilled = {n}")
    else:
        click.echo("reindex: FTS5 rebuilt")


@cli.command()
@click.option("--tail", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.option("--project", default=None, help="Viewer project for audit scope filtering.")
@click.option("--agent", default=None, help="Viewer agent for audit scope filtering.")
def audit(tail: int, as_json: bool, project: str | None, agent: str | None) -> None:
    """Read the audit log."""
    from .scoping import viewer_from

    store = _load_store()
    viewer = viewer_from(
        config_path=store.config_path,
        project=project,
        agent=agent,
    )
    events = list(audit_mod.read_events(store.kb_dir, store=store, viewer=viewer))[-tail:]
    if as_json:
        _emit_json({
            "viewer": {"project": viewer.project, "agent": viewer.agent},
            "events": [e.model_dump(mode="json") for e in events],
        })
        return
    if viewer.project or viewer.agent:
        click.echo(
            f"viewer: project={viewer.project!r} agent={viewer.agent!r}",
            err=True,
        )
    for e in events:
        click.echo(
            f"{e.created_at.isoformat()}  {e.event:30s}  by {e.actor}  objects={e.object_ids}"
        )


# --- cross-session themes -------------------------------------------------


@cli.command(name="detect-themes")
@click.option("--min-sessions", default=None, type=int, help="Minimum sessions for a cluster.")
@click.option("--min-claims", default=None, type=int, help="Minimum claims for a cluster.")
@click.option("--top-k", default=None, type=int, help="Max clusters to return.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--propose", is_flag=True, help="Propose theme pages for each cluster.")
@click.option("--agent", default=None, help="Agent name for proposals.")
def detect_themes_cmd(
    min_sessions: int | None,
    min_claims: int | None,
    top_k: int | None,
    as_json: bool,
    propose: bool,
    agent: str | None,
) -> None:
    """Detect recurring entity clusters across completed sessions."""
    from . import themes

    store = _load_store()
    result = themes.detect_themes(
        store,
        min_sessions=min_sessions,
        min_claims=min_claims,
        top_k=top_k,
    )
    if as_json and not propose:
        _emit_json({
            "clusters": [
                {
                    "entities": c.entities,
                    "claim_ids": c.claim_ids,
                    "session_ids": c.session_ids,
                    "score": c.score,
                    "session_count": c.session_count,
                    "claim_count": c.claim_count,
                }
                for c in result.clusters
            ],
            "config": result.config_used,
        })
        return
    if not result.clusters:
        click.echo("no themes detected")
        return
    if propose:
        actor = agent or _whoami()
        proposed: list[dict] = []
        for cluster in result.clusters:
            try:
                p = themes.propose_theme(store, cluster, proposed_by=actor)
                proposed.append(p)
                if not as_json:
                    click.echo(
                        f"proposed: {p['theme_page_id']} "
                        f"({p['claim_count']} claims, "
                        f"{p['session_count']} sessions)"
                    )
            except Exception as e:
                click.echo(
                    f"skip: {', '.join(cluster.entities)} — {e}",
                    err=True,
                )
        if as_json:
            _emit_json({"proposed": proposed})
        return
    for i, c in enumerate(result.clusters, 1):
        click.echo(
            f"{i}. {', '.join(c.entities)}  "
            f"score={c.score}  sessions={c.session_count}  claims={c.claim_count}"
        )


# --- export / import ------------------------------------------------------


@cli.command()
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
def export(out_path: str) -> None:
    """Bundle the durable KB into a portable .tar.gz."""
    store = _load_store()
    manifest = bundle.export(store.kb_dir, dest=Path(out_path), actor=_whoami())
    _emit_json(
        {
            "bundle_id": manifest["bundle_id"],
            "files": len(manifest["files"]),
            "out": out_path,
        }
    )


@cli.command("export-check")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
def export_check_cmd(bundle_path: str) -> None:
    """Verify every file in a bundle matches its manifest hash."""
    r = bundle.export_check(Path(bundle_path))
    _emit_json(
        {
            "ok": r.ok,
            "bundle_id": r.bundle_id,
            "files_checked": r.files_checked,
            "issues": r.issues,
        }
    )
    sys.exit(0 if r.ok else 1)


@cli.command("import-check")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
def import_check_cmd(bundle_path: str) -> None:
    """Diff a bundle against the destination KB without writing."""
    store = _load_store()
    r = bundle.import_check(store.kb_dir, Path(bundle_path))
    _emit_json(
        {
            "ok": r.ok,
            "bundle_id": r.bundle_id,
            "new_files": r.new_files,
            "conflicts": r.conflicts,
            "identical_files": len(r.identical),
            "issues": r.issues,
        }
    )


@cli.command("import-apply")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--on-conflict",
    default="skip",
    show_default=True,
    type=click.Choice(["skip", "overwrite", "fail"]),
)
def import_apply_cmd(bundle_path: str, on_conflict: str) -> None:
    """Apply a bundle. Default policy is skip — never destructive without explicit overwrite."""
    store = _load_store()
    try:
        r = bundle.import_apply(
            store.kb_dir,
            Path(bundle_path),
            on_conflict=on_conflict,
            actor=_whoami(),
        )
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    # Rebuild the index after a bulk import so search picks up new claims.
    health.rebuild_index(store)
    _emit_json(r)


# --- auto-pr: open N mergeable PRs against any github repo -----------------


@cli.command(name="auto-pr")
@click.argument("repo_url")
@click.option("--workspace", required=True, type=click.Path(),
              help="directory holding (or to hold) the clone/fork.")
@click.option("--count", default=1, show_default=True, type=int,
              help="how many PRs to attempt.")
@click.option("--claude-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--codex-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--issue-label", "issue_labels", multiple=True,
              help="restrict the open-issue source to these labels (repeatable).")
@click.option("--fork-owner", default=None,
              help="fork owner login (default: the authenticated gh user).")
@click.option("--max-revise", default=2, show_default=True, type=int,
              help="max fixer<->verifier revise rounds per item.")
@click.option("--autonomy", default="edit", show_default=True,
              type=click.Choice(["edit", "full"]),
              help="'edit' auto-accepts file edits only (safer default); "
                   "'full' lets the fixer run arbitrary commands "
                   "(bypasses claude's permission prompts).")
@click.option("--dry-run", is_flag=True,
              help="run every stage except git push / gh pr create.")
@click.option("--json", "as_json", is_flag=True)
def auto_pr_cmd(repo_url: str, workspace: str, count: int, claude_effort: str,
                codex_effort: str, issue_labels: tuple[str, ...],
                fork_owner: str | None, max_revise: int, autonomy: str,
                dry_run: bool, as_json: bool) -> None:
    """Open N mergeable PRs against REPO_URL, cross-verified by claude + codex.

    Sources open issues first (then agent-discovered improvements), bootstraps
    a contribution skill from the repo's merged PRs when it ships no guidance,
    and opens a PR only when the repo's own test gate is green and the
    reviewing engine signs off. A sibling tool — it never writes to the KB.
    """
    from . import auto_pr as ap_mod
    try:
        results = ap_mod.run_auto_pr(
            repo_url, workspace, count, claude_effort, codex_effort,
            labels=tuple(issue_labels), fork_owner=fork_owner,
            max_revise=max_revise, autonomy=autonomy, dry_run=dry_run,
        )
    except (ValueError, RuntimeError) as e:
        # surface auto-pr failures as `Error: ...` like the rest of the cli,
        # not a bare traceback. (auto_pr raises ValueError/RuntimeError, not the
        # KB domain types that _cli_errors handles.)
        raise click.ClickException(str(e)) from e
    if as_json:
        _emit_json([
            {"status": r.status, "url": r.url, "fixer": r.fixer,
             "verifier": r.verifier, "title": r.item.title,
             "reason": r.reason, "rounds": r.rounds}
            for r in results
        ])
        return
    if not results:
        click.echo(f"no work items found for {repo_url}", err=True)
        return
    opened = 0
    for r in results:
        if r.status == "opened":
            opened += 1
            click.echo(r.url or "(dry-run: would open)")
        else:
            click.echo(f"skipped: {r.item.title} — {r.reason}", err=True)
    click.echo(f"opened {opened}/{len(results)} PRs", err=True)


# --- dual-solve: run claude + codex on one issue; operator picks a winner ---


@cli.command(name="dual-solve")
@click.argument("issue_url")
@click.option("--claude-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--codex-effort", default="high", show_default=True,
              type=click.Choice(["low", "medium", "high", "max"]))
@click.option("--autonomy", default="edit", show_default=True,
              type=click.Choice(["edit", "full"]),
              help="'edit' auto-accepts file edits only (safer default); "
                   "'full' lets engines run arbitrary commands.")
@click.option("--reason", default=None,
              help="why you picked the winner (skips the interactive prompt).")
@click.option("--no-record", is_flag=True,
              help="keep the chosen branch but propose nothing to the kb.")
@click.option("--dry-run", is_flag=True,
              help="run both engines but make no commits / kb writes.")
@click.option("--sandbox", is_flag=True,
              help="Run claude/codex inside a Docker sandbox image instead of on the host.")
@click.option("--sandbox-image", default=None,
              help="Docker image for --sandbox (default: vouch/coder:latest).")
@click.option("--json", "as_json", is_flag=True,
              help="non-interactive: emit both diffs + metadata, no prompt.")
def dual_solve_cmd(issue_url: str, claude_effort: str, codex_effort: str,
                   autonomy: str, reason: str | None, no_record: bool,
                   dry_run: bool, sandbox: bool, sandbox_image: str | None,
                   as_json: bool) -> None:
    """Run claude + codex on ISSUE_URL; you pick the winning diff.

    Each engine works in its own git worktree on a fresh branch. You compare
    the two diffs, keep one branch, and (unless --no-record) the rationale is
    proposed into the kb for review. A sibling tool to auto-pr; the review
    gate is untouched -- nothing is auto-approved.
    """
    from . import dual_solve as ds_mod
    from .auto_pr import SubprocessRunner
    from .sandbox import DEFAULT_SANDBOX_IMAGE, DockerAgentRunner
    store = _load_store()
    base_runner = SubprocessRunner()
    sandbox_image = sandbox_image or DEFAULT_SANDBOX_IMAGE
    try:
        if sandbox:
            ds_mod._require_engines(
                sandboxed=True, sandbox_image=sandbox_image, runner=base_runner)
        else:
            ds_mod._require_engines()
        root = ds_mod.repo_root(base_runner, Path.cwd())
        runner = (
            DockerAgentRunner(repo_root=root, runner=base_runner, image=sandbox_image)
            if sandbox else base_runner
        )
        issue, candidates, engines = ds_mod.prepare(
            store, issue_url, root, runner,
            claude_effort=claude_effort, codex_effort=codex_effort,
            autonomy=autonomy, dry_run=dry_run,
            on_progress=lambda m: click.echo(m, err=True),
        )
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        _emit_json({
            "issue": {"number": issue.number, "title": issue.title,
                      "url": issue.url},
            "recommendation": ds_mod.recommendation(candidates),
            "candidates": [
                {"engine": c.engine, "branch": c.branch, "ok": c.ok,
                 "error": c.error, "changed_files": ds_mod.changed_files(c.diff),
                 "log": c.log, "diff": c.diff} for c in candidates
            ],
        })
        return

    for c in candidates:
        click.echo(f"\n=== {c.engine} ({c.branch}) ===", err=True)
        if c.log.strip():
            click.echo("--- engine log ---", err=True)
            click.echo(c.log)
        if c.ok:
            if c.log.strip():
                click.echo("--- diff ---", err=True)
            click.echo(c.diff)
        else:
            click.echo(f"(failed: {c.error})", err=True)

    rec = ds_mod.recommendation(candidates)
    if rec.get("reason"):
        label = f"recommendation: {rec['engine']}" if rec.get("engine") \
            else "recommendation: no automatic pick"
        click.echo(f"{label} -- {rec['reason']}", err=True)

    ok = [c for c in candidates if c.ok]
    if not ok:
        raise click.ClickException("both engines failed; nothing to choose")
    choice: str | None
    if len(ok) == 1:
        survivor = ok[0]
        if not click.confirm(
                f"only {survivor.engine} produced a usable diff; proceed with it?",
                default=True):
            ds_mod.finalize(store, root, issue, None, engines, candidates, "",
                            runner, record=False, proposed_by=_whoami())
            raise click.ClickException("aborted; both branches discarded")
        choice = survivor.engine
    else:
        letter = click.prompt("pick a winner [c]laude / [x]codex / [n]either",
                              type=click.Choice(["c", "x", "n"]), default="c")
        choice = {"c": "claude", "x": "codex", "n": None}[letter]

    chosen = next((c for c in candidates if c.engine == choice), None)
    if reason is None and chosen is not None and not no_record and not dry_run:
        reason = click.prompt("one line: why this solution", default="")

    try:
        ids = ds_mod.finalize(
            store, root, issue, chosen, engines, candidates, reason or "", runner,
            record=not no_record and not dry_run, proposed_by=_whoami(),
        )
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(f"failed to record/clean up: {e}") from e
    if chosen is None:
        click.echo("kept neither; both branches discarded", err=True)
        return
    click.echo(f"kept {chosen.branch}", err=True)
    for pid in ids:
        click.echo(f"proposed {pid} -- review with `vouch approve {pid}`", err=True)


# --- sync ------------------------------------------------------------------


@cli.command("sync-check")
@click.argument("source_path", type=click.Path(exists=True))
def sync_check_cmd(source_path: str) -> None:
    """Compare another .vouch directory or bundle without writing."""
    store = _load_store()
    try:
        r = sync_mod.sync_check(store.kb_dir, Path(source_path))
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    _emit_json(asdict(r))


@cli.command("sync-apply")
@click.argument("source_path", type=click.Path(exists=True))
@click.option(
    "--on-conflict",
    default="fail",
    show_default=True,
    type=click.Choice(["fail", "skip", "propose"]),
)
def sync_apply_cmd(source_path: str, on_conflict: str) -> None:
    """Apply non-conflicting files from another .vouch directory or bundle."""
    store = _load_store()
    try:
        r = sync_mod.sync_apply(
            store.kb_dir,
            Path(source_path),
            on_conflict=on_conflict,
            actor=_whoami(),
        )
    except (RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    health.rebuild_index(store)
    _emit_json(r)


# --- diff -----------------------------------------------------------------


@cli.command()
@click.argument("old_id")
@click.argument("new_id", required=False)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the diff as JSON.")
def diff(old_id: str, new_id: str | None, as_json: bool) -> None:
    """Show what changed between two claim or two page revisions.

    NEW_ID is optional for a claim that has been superseded: it resolves to
    ``superseded_by`` automatically.
    """
    from .diff import diff_artifacts

    store = _load_store()
    with _cli_errors():
        d = diff_artifacts(store, old_id, new_id)
    if as_json:
        _emit_json(asdict(d))
        return
    if not d.changes and not d.text_diff:
        click.echo("no differences")
        return
    click.echo(f"diff {d.kind} {d.old_id} → {d.new_id}")
    for c in d.changes:
        click.echo(f"  {c.field}: {c.old} → {c.new}")
    if d.text_diff:
        label = "body" if d.kind == "page" else "text"
        click.echo(f"  {label}:")
        for line in d.text_diff:
            click.echo(f"    {line}")


# --- serve ----------------------------------------------------------------


@cli.command()
@click.option(
    "--transport", default="stdio", show_default=True, type=click.Choice(["stdio", "jsonl", "http"])
)
@click.option(
    "--host", default="127.0.0.1", show_default=True, help="HTTP bind host (transport=http)."
)
@click.option(
    "--port", default=None, type=int, help="HTTP bind port (transport=http; default 8731)."
)
@click.option(
    "--token",
    default=None,
    envvar="VOUCH_HTTP_TOKEN",
    help="Bearer token for HTTP /rpc + /mcp (or env VOUCH_HTTP_TOKEN). "
    "Combine with --config for a multi-token accept-list. "
    "Required to bind a non-loopback host.",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Path to a config.yaml with a `serve:` section "
    "(default: .vouch/config.yaml if present, then ./config.yaml). "
    "Supplies `bearer_tokens:` (list) or `bearer_token: env:VAR`.",
)
@click.option(
    "--allow-public",
    is_flag=True,
    help="Permit binding a non-loopback host (requires at least one token).",
)
def serve(
    transport: str,
    host: str,
    port: int | None,
    token: str | None,
    config_path: str | None,
    allow_public: bool,
) -> None:
    """Run the MCP server (stdio), the JSONL tool server, or the HTTP server.

    HTTP transport surfaces three protocols against the same kb.* surface:

    \b
        POST /mcp        MCP-over-Streamable-HTTP (Claude.ai Custom Connector,
                         Claude mobile, Managed Agents, Messages-API
                         mcp_servers, Computer Use)
        POST /messages   alias for /mcp (older Claude surfaces)
        POST /rpc        vouch-native JSONL envelope (legacy clients)

    GET /health, /healthz, and /capabilities are always unauthenticated.
    """
    _load_store()  # fail fast with a clear message if no .vouch/ KB is present

    if transport == "stdio":
        from .server import run_stdio

        run_stdio()
        return
    if transport == "jsonl":
        from .jsonl_server import run_jsonl

        run_jsonl()
        return

    from .http_server import DEFAULT_PORT, ServeConfigError, load_serve_config, run_http

    bind_port = port if port is not None else DEFAULT_PORT

    # Locate config.yaml: explicit --config wins, else look for project-local
    # .vouch/config.yaml, then ./config.yaml. Missing file is fine — the CLI
    # is fully usable with --token alone.
    cfg_candidates: list[Path]
    if config_path:
        cfg_candidates = [Path(config_path)]
    else:
        cfg_candidates = [Path(".vouch/config.yaml"), Path("config.yaml")]
    tokens: list[str] = []
    for cand in cfg_candidates:
        if cand.exists():
            try:
                serve_cfg = load_serve_config(cand)
            except ServeConfigError as e:
                raise click.ClickException(f"serve config {cand}: {e}") from e
            tokens = list(serve_cfg.tokens)
            break

    try:
        run_http(host, bind_port, token=token, tokens=tokens, allow_public=allow_public)
    except RuntimeError as e:
        # e.g. the non-loopback bind guard — show a clean Error: line.
        raise click.ClickException(str(e)) from e


# --- pr-cache: dedup PR raises against a target repo ----------------------


@cli.group(name="pr-cache")
def pr_cache_group() -> None:
    """Cache a target repo's merged/closed PRs to prevent duplicate PR raises.

    Workflow:

    \b
        vouch pr-cache build https://github.com/owner/repo --analyze-closed
        vouch pr-cache check owner/repo --topic "fix doc preview accessible"
        vouch pr-cache show owner/repo --state closed --json
    """


@pr_cache_group.command("build")
@click.argument("repo")
@click.option(
    "--state",
    type=click.Choice(["merged", "closed", "all"]),
    default="all",
    show_default=True,
    help="Which PR states to fetch.",
)
@click.option(
    "--limit", type=int, default=200, show_default=True, help="Max PRs per state to fetch from gh."
)
@click.option(
    "--analyze-closed",
    is_flag=True,
    help="Run Claude/Anthropic to summarise WHY each closed-not-merged "
    "PR was closed (uses local `claude` CLI if present, else "
    "ANTHROPIC_API_KEY). Skipped silently when neither is set.",
)
@click.option(
    "--reanalyze",
    is_flag=True,
    help="Re-run close-reason analysis even if a previous result is cached.",
)
@click.option(
    "--analyzer",
    type=click.Choice(["auto", "claude-cli", "anthropic-api", "none"]),
    default="auto",
    show_default=True,
    help="Which close-reason analyzer to prefer.",
)
@click.option(
    "--no-fetch-files",
    is_flag=True,
    help="Skip per-PR file-list fetch (faster, but dedup by file overlap stops working).",
)
@click.option(
    "--cache-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Override cache directory (also env VOUCH_PR_CACHE_DIR).",
)
def pr_cache_build(
    repo: str,
    state: str,
    limit: int,
    analyze_closed: bool,
    reanalyze: bool,
    analyzer: str,
    no_fetch_files: bool,
    cache_dir: str | None,
) -> None:
    """Fetch merged/closed PRs for REPO and upsert into the local cache."""
    with _cli_errors():
        ref = prc_mod.parse_repo(repo)
        try:
            result = prc_mod.build(
                ref,
                state=state,
                limit=limit,
                analyze_closed=analyze_closed,
                reanalyze=reanalyze,
                analyzer=analyzer,
                cache_dir=Path(cache_dir) if cache_dir else None,
                fetch_files=not no_fetch_files,
            )
        except prc_mod.GHError as e:
            raise click.ClickException(str(e)) from e
    _emit_json(
        {
            "repo": ref.slug,
            "fetched": result.fetched,
            "new": result.new,
            "updated": result.updated,
            "analyzed": result.analyzed,
            "skipped_analysis": result.skipped_analysis,
            "cache_path": str(result.path),
        }
    )


@pr_cache_group.command("check")
@click.argument("repo")
@click.option(
    "--topic",
    required=True,
    help="Short description of the PR you're about to raise (title-like text).",
)
@click.option(
    "--files",
    default="",
    help="Comma-separated list of paths the planned PR would touch (boosts dedup precision).",
)
@click.option(
    "--min-score",
    default=0.15,
    show_default=True,
    type=float,
    help="Minimum similarity (0..1) for a cached PR to count as a duplicate signal.",
)
@click.option("--top-k", default=5, show_default=True, type=int)
@click.option(
    "--cache-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Override cache directory (also env VOUCH_PR_CACHE_DIR).",
)
def pr_cache_check(
    repo: str, topic: str, files: str, min_score: float, top_k: int, cache_dir: str | None
) -> None:
    """Look up cached PRs similar to TOPIC; warns of likely-duplicate raises."""
    with _cli_errors():
        ref = prc_mod.parse_repo(repo)
        path = prc_mod.cache_path_for(ref, Path(cache_dir) if cache_dir else None)
        cache = prc_mod.load_cache(path)
        file_list = [f.strip() for f in files.split(",") if f.strip()]
        cands = prc_mod.check_duplicates(
            cache,
            topic=topic,
            files=file_list,
            min_score=min_score,
            top_k=top_k,
        )
    _emit_json(
        {
            "repo": ref.slug,
            "cache_path": str(path),
            "cache_size": len(cache),
            "topic": topic,
            "files": file_list,
            "candidates": [c.as_json() for c in cands],
            # 0.7 (= 70 % of topic tokens contained in a cached PR's title+body)
            # is the threshold for "almost certainly the same idea." Below that,
            # surface as a soft signal the caller should eyeball before raising.
            "verdict": "likely_duplicate"
            if any(c.score >= 0.70 for c in cands)
            else "review_candidates"
            if cands
            else "no_match",
        }
    )


@pr_cache_group.command("show")
@click.argument("repo")
@click.option(
    "--state", type=click.Choice(["merged", "closed", "all"]), default="all", show_default=True
)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.option(
    "--cache-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Override cache directory (also env VOUCH_PR_CACHE_DIR).",
)
def pr_cache_show(repo: str, state: str, limit: int, as_json: bool, cache_dir: str | None) -> None:
    """List the cached PRs for REPO."""
    with _cli_errors():
        ref = prc_mod.parse_repo(repo)
        path = prc_mod.cache_path_for(ref, Path(cache_dir) if cache_dir else None)
        cache = prc_mod.load_cache(path)
    records = sorted(cache.values(), key=lambda r: r.number, reverse=True)
    if state != "all":
        records = [r for r in records if r.state == state]
    records = records[:limit]
    if as_json:
        _emit_json(
            {
                "repo": ref.slug,
                "cache_path": str(path),
                "count": len(records),
                "prs": [
                    {
                        "number": r.number,
                        "state": r.state,
                        "title": r.title,
                        "url": r.url,
                        "merged_at": r.merged_at,
                        "closed_at": r.closed_at,
                        "files": r.files,
                        "labels": r.labels,
                        "close_analysis": (asdict(r.close_analysis) if r.close_analysis else None),
                    }
                    for r in records
                ],
            }
        )
        return
    if not records:
        click.echo(f"no cached PRs for {ref.slug} in {path}")
        return
    click.echo(f"{ref.slug}  ({len(records)} PRs from {path})")
    for r in records:
        when = r.merged_at or r.closed_at or ""
        marker = "✓" if r.state == "merged" else "✗"
        click.echo(f"  {marker} #{r.number:<6}  {r.state:<7}  {when[:10]:<10}  {r.title}")
        if r.close_analysis and r.close_analysis.reason:
            click.echo(f"      reason: {r.close_analysis.reason}")
            for nrep in r.close_analysis.do_not_repeat[:3]:
                click.echo(f"      ✗ avoid: {nrep}")


# --- install-mcp: drop the right adapter files into a project tree --------


@cli.command(name="install-mcp", context_settings={"ignore_unknown_options": False})
@click.argument("host", required=False)
@click.option("--list", "list_hosts", is_flag=True, help="List available hosts and exit.")
@click.option(
    "--path",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Target project root.",
)
@click.option(
    "--target",
    "target_alias",
    default=None,
    type=click.Path(file_okay=False),
    help="Alias for --path (per issue #179 spec).",
)
@click.option(
    "--tier",
    default="T4",
    show_default=True,
    type=click.Choice(["T1", "T2", "T3", "T4"]),
    help="Adoption tier: T1 = MCP wire only, "
    "T2 = +CLAUDE.md/AGENTS.md, T3 = +slash commands, "
    "T4 = +host hooks/settings. Tiers stack.",
)
def install_mcp(
    host: str | None, list_hosts: bool, path: str, target_alias: str | None, tier: str
) -> None:
    """Install vouch into HOST (claude-code, cursor, …) idempotently.

    \b
    Examples:
      vouch install-mcp --list              # show known hosts
      vouch install-mcp claude-code         # write T1..T4 into cwd
      vouch install-mcp cursor --tier T2    # stop at AGENTS.md
      vouch install-mcp claude-desktop      # drop a paste-ready config
      vouch install-mcp windsurf --path /abs/path/to/project
    """
    hosts = install_mod.available_adapters()
    if list_hosts:
        if not hosts:
            click.echo("(no adapters installed alongside this vouch install)")
            return
        click.echo("Available MCP host adapters:")
        for h in hosts:
            click.echo(f"  - {h}")
        click.echo("")
        click.echo("Install one with: vouch install-mcp <host> [--tier T1|T2|T3|T4]")
        return

    if host is None:
        raise click.ClickException(
            "missing HOST; run `vouch install-mcp --list` to see the catalogue"
        )

    target = Path(target_alias or path).resolve()
    try:
        result = install_mod.install(host, target=target, tier=tier)
    except install_mod.AdapterError as e:
        raise click.ClickException(str(e)) from e

    for f in result.written:
        click.echo(f"  + {f}")
    for f in result.appended:
        click.echo(f"  ~ {f}  (appended fenced block)")
    for f in result.merged:
        click.echo(f"  ~ {f}  (merged into existing)")
    for f in result.skipped:
        click.echo(f"  · {f}  (already present)")
    click.echo(
        f"Done — {len(result.written)} written, "
        f"{len(result.appended)} appended, {len(result.merged)} merged, "
        f"{len(result.skipped)} skipped "
        f"under {target}"
    )


# --- sync: bidirectional vouch <-> Obsidian-style vault -------------------


@cli.command(name="sync")
@click.option(
    "--vault",
    "vault_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Path to an Obsidian-style markdown vault. Mirroring happens under <vault>/vouch/.",
)
@click.option(
    "--direction",
    default="both",
    show_default=True,
    type=click.Choice(["both", "forward", "backward"]),
    help="forward = vault→KB (file page-edit proposals), "
    "backward = KB→vault (mirror approved pages + claim stubs).",
)
@click.option(
    "--actor",
    default="vault-sync",
    show_default=True,
    help="Proposer name recorded on every page-edit proposal.",
)
@click.option(
    "--watch", is_flag=True, help="Stay alive and re-sync the vault every --poll seconds."
)
@click.option(
    "--poll",
    default=2.0,
    show_default=True,
    type=float,
    help="Polling interval (seconds) when --watch is set.",
)
def sync_cmd(vault_dir: str, direction: str, actor: str, watch: bool, poll: float) -> None:
    """Sync the KB with an Obsidian-compatible markdown vault (VEP-style #181).

    \b
    Forward (vault→KB):
        Edits to <vault>/vouch/pages/<id>.md become page-edit proposals
        in .vouch/proposed/, citing a vault:<relpath> source so the review
        gate can see exactly which bytes triggered the proposal.

    \b
    Backward (KB→vault):
        Approved pages mirror into <vault>/vouch/pages/. Approved claims
        get a markdown stub under <vault>/vouch/claims/ with Obsidian
        wikilink backlinks to citing pages so the graph view connects them.

    \b
    Re-runs are idempotent — only real edits become proposals, the rest is
    a no-op. Add --watch to keep a polling loop alive while you edit.
    """
    store = _load_store()
    vault_path = Path(vault_dir).resolve()
    try:
        if watch:
            click.echo(
                f"Watching {vault_path} every {poll}s "
                f"(direction={direction}, actor={actor}); Ctrl-C to stop."
            )
            ticks = vault_sync_mod.watch_vault(
                store,
                vault_path,
                direction=direction,
                actor=actor,
                poll_interval=poll,
            )
            click.echo(f"Stopped after {ticks} tick(s).")
            return
        result = vault_sync_mod.sync_vault(
            store,
            vault_path,
            direction=direction,
            actor=actor,
        )
    except vault_sync_mod.VaultSyncError as e:
        raise click.ClickException(str(e)) from e

    for pid in result.pages_mirrored:
        click.echo(f"  ↓ pages/{pid}.md  (mirrored)")
    for cid in result.claims_mirrored:
        click.echo(f"  ↓ claims/{cid}.md  (mirrored)")
    for pid in result.pages_proposed:
        click.echo(f"  ↑ pages/{pid}  (proposal filed)")
    for rel in result.pages_skipped_unchanged:
        click.echo(f"  · {rel}  (unchanged)")
    for rel in result.pages_skipped_unknown_id:
        click.echo(f"  ! {rel}  (skipped — could not parse page id)")
    click.echo(
        f"Done — {len(result.pages_mirrored)} pages and "
        f"{len(result.claims_mirrored)} claims mirrored, "
        f"{len(result.pages_proposed)} proposals filed."
    )


# --- review-ui: browser-based review console -----------------------------


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def _resolve_auth_token(auth: str | None) -> str | None:
    """Turn the ``--auth`` option into a concrete token (or ``None``).

    * ``None``          -> no auth (only allowed on a loopback bind).
    * ``"generate"``    -> mint a random token and print it once.
    * ``"env"``         -> read ``VOUCH_REVIEW_TOKEN`` from the environment.
    * any other string  -> use it verbatim as the bearer token.
    """
    if auth is None:
        return None
    if auth == "generate":
        import secrets

        token = secrets.token_urlsafe(24)
        click.echo(f"Generated review token: {token}")
        return token
    if auth == "env":
        env_token = os.environ.get("VOUCH_REVIEW_TOKEN")
        if not env_token:
            raise click.ClickException(
                "--auth env: VOUCH_REVIEW_TOKEN is not set in the environment"
            )
        return env_token
    return auth


@cli.command(name="console")
@click.option(
    "--bind",
    "bind",
    default="127.0.0.1:5173",
    show_default=True,
    help="host:port to bind. A non-loopback host (e.g. 0.0.0.0) also "
    "requires --allow-remote so the proxy bridge isn't exposed openly.",
)
@click.option(
    "--allow-remote",
    is_flag=True,
    help="Drop the loopback guard on the /proxy bridge. Only for a deployment "
    "behind its own auth — a same-origin page could otherwise drive a "
    "local reviewer's backends.",
)
@click.option(
    "--open-browser/--no-open-browser",
    default=True,
    show_default=True,
    help="Open the browser to the console on startup.",
)
def console(bind: str, allow_remote: bool, open_browser: bool) -> None:
    """Serve the vouch web console (the React review UI) locally.

    Ships the built SPA and a same-origin /proxy bridge to your
    `vouch serve --transport http` backends — one `pip install 'vouch-kb[web]'`,
    no node. Add a backend from the connect dialog in the UI.
    """
    from .web import _require_console_deps

    try:
        _require_console_deps()
    except ImportError as exc:
        raise click.ClickException(str(exc)) from exc

    from .web.console import ConsoleError, resolve_console_dir, serve_console

    host, sep, port_raw = bind.partition(":")
    if not sep:
        raise click.ClickException(f"invalid --bind {bind!r}; expected host:port")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise click.ClickException(f"invalid port in --bind {bind!r}") from exc
    host = host or "127.0.0.1"
    if host not in ("127.0.0.1", "::1", "localhost") and not allow_remote:
        raise click.ClickException(
            f"--bind {bind} is non-loopback; pass --allow-remote to expose the "
            "proxy bridge (only behind your own auth)."
        )

    url = f"http://{host}:{port}/"
    if open_browser and resolve_console_dir() is not None:
        import threading
        import webbrowser

        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    click.echo(f"vouch console → {url}")
    try:
        serve_console(host=host, port=port, allow_remote=allow_remote)
    except ConsoleError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command(name="review-ui")
@click.option(
    "--bind",
    "bind",
    default="127.0.0.1:7780",
    show_default=True,
    help="host:port to bind. A non-loopback host (e.g. 0.0.0.0) "
    "requires --auth so the approve surface isn't exposed "
    "unauthenticated.",
)
@click.option(
    "--auth",
    default=None,
    help="Bearer-token mode: a literal token, 'generate' (mint a "
    "random one and print it), or 'env' (read "
    "VOUCH_REVIEW_TOKEN). Required for non-loopback binds.",
)
@click.option(
    "--reviewer",
    "reviewer",
    default="web-reviewer",
    show_default=True,
    help="Identity recorded in the audit log for token-authed approve/reject decisions.",
)
@click.option(
    "--page-size", default=None, type=int, help="Queue page size (server-side pagination)."
)
@click.option(
    "--kb",
    "kb_root",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="KB root (defaults to the nearest .vouch/ above cwd).",
)
@click.option(
    "--open-browser/--no-open-browser",
    default=True,
    show_default=True,
    help="Open the browser to the queue on startup.",
)
@click.option(
    "--allow-dual-solve",
    is_flag=True,
    help="Mount the dual-solve runner SPA (spawns claude+codex; edit-only). "
         "Off by default; the server must run inside the target git repo.",
)
@click.option(
    "--dual-solve-sandbox",
    is_flag=True,
    help="Run dual-solve claude/codex invocations inside a Docker sandbox image.",
)
@click.option(
    "--dual-solve-sandbox-image",
    default=None,
    help="Docker image for --dual-solve-sandbox (default: vouch/coder:latest).",
)
def review_ui(
    bind: str,
    auth: str | None,
    reviewer: str,
    page_size: int | None,
    kb_root: str | None,
    open_browser: bool,
    allow_dual_solve: bool,
    dual_solve_sandbox: bool,
    dual_solve_sandbox_image: str | None,
) -> None:
    """Run the browser-based review console (issue #194).

    \b
    Examples:
      vouch review-ui                              # 127.0.0.1:7780, open browser
      vouch review-ui --bind 127.0.0.1:8000
      vouch review-ui --no-open-browser            # ssh / headless friendly
      vouch review-ui --bind 0.0.0.0:7780 --auth generate   # team mode
      VOUCH_REVIEW_TOKEN=… vouch review-ui --bind 0.0.0.0:7780 --auth env
    """
    if ":" not in bind:
        raise click.ClickException(f"--bind must be host:port (got {bind!r})")
    host, _, port_str = bind.rpartition(":")
    try:
        port = int(port_str)
    except ValueError as e:
        raise click.ClickException(f"invalid port in --bind: {port_str!r}") from e

    token = _resolve_auth_token(auth)

    # Refuse a non-loopback bind without a bearer token — exposing an
    # unauthenticated approve surface on the network would let anyone on the
    # LAN mutate the KB. Same posture as the HTTP transport (#1).
    is_loopback = host in _LOOPBACK_HOSTS
    if not is_loopback and token is None:
        raise click.ClickException(
            f"--bind {bind!r} is non-loopback; pass --auth (a token, "
            "'generate', or 'env') so the approve surface requires a "
            "Bearer token. Refusing to expose an unauthenticated gate."
        )

    try:
        from . import web as web_pkg
    except ImportError as e:
        raise click.ClickException(str(e)) from e

    try:
        app = web_pkg.create_app(
            kb_root, auth_token=token, auth_label=reviewer, page_size=page_size,
            allow_dual_solve=allow_dual_solve,
            dual_solve_sandbox=dual_solve_sandbox,
            dual_solve_sandbox_image=dual_solve_sandbox_image,
        )
    except (FileNotFoundError, RuntimeError) as e:
        raise click.ClickException(str(e)) from e

    try:
        import uvicorn
    except ImportError as e:
        raise click.ClickException(
            "vouch review-ui needs the [web] extra. Install with: pip install 'vouch-kb[web]'"
        ) from e

    auth_note = " (Bearer auth on)" if token else ""
    if open_browser and is_loopback:
        # Lazy-import webbrowser; some CI envs (headless) don't have a default
        # browser configured and webbrowser.open(encoding="utf-8") returns False rather than
        # raising — that's fine, the URL is also printed to stdout. When auth
        # is on, hand the browser the token once via ?token= so it can stash it.
        import threading
        import webbrowser

        suffix = f"?token={token}" if token else ""
        url = f"http://{host}:{port}/{suffix}"
        click.echo(f"vouch review-ui running at http://{host}:{port}/{auth_note}")
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    else:
        click.echo(f"vouch review-ui running at http://{host}:{port}/{auth_note}")

    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command("openclaw-rpc")
def openclaw_rpc() -> None:
    """OpenClaw bridge: one JSON envelope on stdin, one on stdout (context engine)."""
    from .openclaw import rpc as openclaw_rpc_mod

    raise SystemExit(openclaw_rpc_mod.run_stdio())


if __name__ == "__main__":
    cli()
