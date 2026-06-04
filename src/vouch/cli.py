"""vouch CLI.

Agents propose writes via the MCP / JSONL servers; humans use this CLI to
review, lifecycle-manage, lint, export and import. All surfaces share the
same storage + audit + index layer.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click
import yaml

from . import __version__, bundle, health
from . import audit as audit_mod
from . import lifecycle as life
from . import migrations as migrations_mod
from . import pr_cache as prc_mod
from . import sessions as sess_mod
from . import sync as sync_mod
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
from .proposals import (
    EXPIRE_ACTOR,
    ProposalError,
    check_approvable,
    expire_pending,
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
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
    return (
        os.environ.get("VOUCH_AGENT")
        or os.environ.get("VOUCH_USER")
        or getpass.getuser()
    )


def _emit_json(obj) -> None:
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


_SEVERITY_STYLE = {
    "error": {"marker": "✗", "fg": "red"},
    "warning": {"marker": "!", "fg": "yellow"},
    "info": {"marker": "·", "fg": "cyan"},
}


def _print_findings(findings: list) -> None:
    for f in findings:
        style = _SEVERITY_STYLE.get(f.severity, {"marker": "?", "fg": None})
        line = f"{style['marker']} [{f.code}] {f.message}"
        _echo(_style(line, fg=style["fg"]))


def _progress_cb(verb: str) -> Callable[[str], None] | None:
    # Progress is for humans watching a terminal; stay silent in pipes/CI/tests
    # so machine output and captured test stdout aren't polluted. Writes to
    # stderr so it never lands in piped stdout.
    if not sys.stderr.isatty():
        return None

    def cb(step: str) -> None:
        click.echo(_style(f"  … {verb} {step}", fg="cyan"), err=True)

    return cb


@click.group()
@click.version_option(__version__, prog_name="vouch")
def cli() -> None:
    """vouch — git-native, review-gated knowledge base for LLM agents."""
    configure_logging()


# --- bootstrap ------------------------------------------------------------


@cli.command()
@click.option("--path", default=".", type=click.Path(file_okay=False), show_default=True)
@click.option("--template", default=DEFAULT_TEMPLATE, show_default=True,
              help="Starter pack to seed (e.g. gittensor for SN74 context).")
def init(path: str, template: str) -> None:
    """Initialise a .vouch/ knowledge base at PATH."""
    if template not in available_templates():
        raise click.ClickException(
            f"unknown template '{template}' "
            f"(available: {', '.join(available_templates())})"
        )
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = KBStore.init(root)
    if template == DEFAULT_TEMPLATE:
        seed = seed_starter_kb(store, approved_by=_whoami())
        health.rebuild_index(store)
        audit_mod.log_event(store.kb_dir, event="kb.init", actor=_whoami())
        click.echo(f"Initialised KB at {store.kb_dir}")
        if seed.created_anything:
            click.echo(f"Seeded starter claim: {seed.claim_id}")
        else:
            click.echo("Starter claim already present.")
    else:
        result = TEMPLATES[template](store, approved_by=_whoami())
        health.rebuild_index(store)
        audit_mod.log_event(store.kb_dir, event="kb.init", actor=_whoami())
        click.echo(f"Initialised KB at {store.kb_dir}")
        if result.created_anything:
            click.echo(
                f"Seeded {result.template} template: "
                f"{len(result.created)} artifact(s)"
            )
        else:
            click.echo(f"{result.template} template already present.")
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
    _echo(f"KB at {_style(str(s['kb_dir']), bold=True)}")
    _echo(
        f"  durable: {_style(str(s['claims']), fg='cyan')} claims  •  "
        f"{_style(str(s['pages']), fg='cyan')} pages  •  "
        f"{_style(str(s['sources']), fg='cyan')} sources  •  "
        f"{_style(str(s['entities']), fg='cyan')} entities  •  "
        f"{_style(str(s['relations']), fg='cyan')} relations"
    )
    pending = s["pending_proposals"]
    pending_str = _style(str(pending), fg="yellow" if pending else "green")
    _echo(f"  pending: {pending_str} proposals")
    present = s["index_present"]
    index_str = (
        _style("present", fg="green") if present else _style("missing", fg="red")
    )
    _echo(f"  audit:   {_style(str(s['audit_events']), fg='cyan')} events  •  "
          f"index: {index_str}")


def _findings_json(report) -> list[dict[str, Any]]:
    return [
        {"severity": f.severity, "code": f.code, "message": f.message,
         "object_ids": list(getattr(f, "object_ids", []) or [])}
        for f in report.findings
    ]


@cli.command()
@click.option("--stale-days", default=180, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit findings as JSON.")
def lint(stale_days: int, as_json: bool) -> None:
    """Surface user-actionable problems: broken citations, stale claims, dangling refs."""
    store = _load_store()
    report = health.lint(store, stale_after_days=stale_days)
    if as_json:
        _emit_json({"ok": report.ok, "findings": _findings_json(report)})
        sys.exit(0 if report.ok else 1)
    _print_findings(report.findings)
    if not report.findings:
        _echo(_style("clean", fg="green"))
    sys.exit(0 if report.ok else 1)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit findings as JSON.")
def doctor(as_json: bool) -> None:
    """Full health sweep: lint + source verification + index check."""
    store = _load_store()
    report = health.doctor(store, on_progress=_progress_cb("verifying"))
    if as_json:
        _emit_json({
            "ok": report.ok, "counts": report.counts,
            "findings": _findings_json(report),
        })
        sys.exit(0 if report.ok else 1)
    _print_findings(report.findings)
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


@cli.command()
@click.option("--check", "check_only", is_flag=True, help="Only check whether migration is needed.")
@click.option("--dry-run", is_flag=True, help="Show planned changes without writing.")
@click.option("--to-version", type=int, default=None, help="Target KB format version.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def migrate(
    check_only: bool,
    dry_run: bool,
    to_version: int | None,
    as_json: bool,
) -> None:
    """Upgrade the on-disk .vouch/ layout to the supported format."""
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
        preview = (
            pr.payload.get("text")
            or pr.payload.get("title")
            or pr.payload.get("name")
            or "—"
        )
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

        reason = (
            click.prompt("Approval reason", default="", show_default=False).strip()
            or None
        )
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


@cli.command()
@click.argument("proposal_ids", nargs=-1, required=True)
@click.option("--reason", default=None)
@click.option(
    "--keep-going", is_flag=True,
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


def _expire_row(proposal: Proposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "kind": proposal.kind.value,
        "proposed_by": proposal.proposed_by,
        "proposed_at": proposal.proposed_at.isoformat(),
    }


@cli.command()
@click.option("--apply", is_flag=True, help="Expire stale proposals (default is dry-run).")
@click.option("--days", type=int, default=None,
              help="Override review.expire_pending_after_days for this run.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def expire(apply: bool, days: int | None, as_json: bool) -> None:
    """Garbage-collect pending proposals older than the configured threshold."""
    store = _load_store()
    result = expire_pending(
        store, apply=apply, expired_by=EXPIRE_ACTOR, days=days,
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
        click.echo(
            f"no stale pending proposals (threshold: {result.threshold_days} days)"
        )
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
        f"expired {len(result.expired)} proposal(s) "
        f"(threshold: {result.threshold_days} days)"
    )
    for pr in result.expired:
        click.echo(f"  {pr.id}  [{pr.kind.value}]")


# --- proposal-from-CLI shortcuts -----------------------------------------


@cli.command(name="propose-claim")
@click.option("--text", required=True)
@click.option("--source", "sources", multiple=True, required=True,
              help="Source or evidence id. Repeatable.")
@click.option("--type", "claim_type", default="observation", show_default=True)
@click.option("--confidence", default=0.7, show_default=True, type=float)
@click.option("--rationale", default=None)
@click.option("--tag", "tags", multiple=True)
def propose_claim_cmd(text: str, sources: tuple[str, ...], claim_type: str,
                      confidence: float, rationale: str | None,
                      tags: tuple[str, ...]) -> None:
    store = _load_store()
    with _cli_errors():
        pr = propose_claim(
            store, text=text, evidence=list(sources),
            proposed_by=_whoami(), claim_type=claim_type,
            confidence=confidence, tags=list(tags), rationale=rationale,
        )
    click.echo(pr.id)


@cli.command(name="propose-page")
@click.option("--title", required=True)
@click.option("--body", default="", help="Page body. Use `-` to read from stdin.")
@click.option("--type", "page_type", default="concept", show_default=True)
@click.option("--claim", "claims", multiple=True)
@click.option("--entity", "entities", multiple=True)
def propose_page_cmd(title: str, body: str, page_type: str,
                     claims: tuple[str, ...], entities: tuple[str, ...]) -> None:
    store = _load_store()
    if body == "-":
        body = sys.stdin.read()
    with _cli_errors():
        pr = propose_page(
            store, title=title, body=body, page_type=page_type,
            claim_ids=list(claims), entity_ids=list(entities),
            proposed_by=_whoami(),
        )
    click.echo(pr.id)


@cli.command(name="propose-entity")
@click.option("--name", required=True)
@click.option("--type", "entity_type", required=True)
@click.option("--alias", "aliases", multiple=True)
@click.option("--description", default=None)
def propose_entity_cmd(name: str, entity_type: str, aliases: tuple[str, ...],
                       description: str | None) -> None:
    store = _load_store()
    with _cli_errors():
        pr = propose_entity(
            store, name=name, entity_type=entity_type,
            aliases=list(aliases), description=description, proposed_by=_whoami(),
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
            store, src=src, relation=relation, target=target,
            confidence=confidence, proposed_by=_whoami(),
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
def source_add(path: str, title: str | None, url: str | None,
               source_type: str) -> None:
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
        store.kb_dir, event="source.add", actor=_whoami(), object_ids=[src.id],
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


# --- lifecycle ------------------------------------------------------------


@cli.command()
@click.argument("old_claim_id")
@click.argument("new_claim_id")
def supersede(old_claim_id: str, new_claim_id: str) -> None:
    """Mark OLD as superseded by NEW."""
    store = _load_store()
    with _cli_errors():
        life.supersede(store, old_claim_id=old_claim_id,
                       new_claim_id=new_claim_id, actor=_whoami())
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
    "--agent", default=None,
    help="Agent id (defaults to $VOUCH_AGENT or current user).",
)
@click.option("--task", default=None)
@click.option("--note", default=None)
def session_start_cmd(agent: str | None, task: str | None, note: str | None) -> None:
    store = _load_store()
    sess = sess_mod.session_start(
        store, agent=agent or os.environ.get("VOUCH_AGENT") or _whoami(),
        task=task, note=note,
    )
    click.echo(sess.id)


@session.command("end")
@click.argument("session_id")
@click.option("--note", default=None)
def session_end_cmd(session_id: str, note: str | None) -> None:
    store = _load_store()
    with _cli_errors():
        sess = sess_mod.session_end(store, session_id, note=note)
    _emit_json({"session": sess.id, "proposals": sess.proposal_ids})


@cli.command()
@click.argument("session_id")
@click.option("--no-page", is_flag=True, help="Skip the session-summary page.")
def crystallize(session_id: str, no_page: bool) -> None:
    """Approve every pending proposal in a session (and write a summary page)."""
    store = _load_store()
    with _cli_errors():
        result = sess_mod.crystallize(
            store, session_id, approver=_whoami(), write_summary_page=not no_page,
        )
    _emit_json(result)
    n_approved = len(result["approved"])
    n_failed = len(result["failures"])
    total = n_approved + n_failed
    if total > 0 and n_failed == total:
        click.echo(
            f"error: all {total} proposal(s) failed to approve — "
            f"crystallize aborted",
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
@click.option("--semantic/--no-semantic", default=None,
              help="Force semantic backend (alias for --backend embedding).")
@click.option(
    "--backend",
    type=click.Choice(["auto", "embedding", "fts5", "substring", "hybrid"]),
    default="auto", show_default=True,
)
@click.option("--min-score", default=0.0, show_default=True, type=float)
@click.option("--rerank/--no-rerank", default=False)
@click.option("--hyde/--no-hyde", default=False)
@click.option("--explain/--no-explain", default=False)
@click.option("--json", "as_json", is_flag=True, help="Emit hits as JSON.")
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
) -> None:
    """Search the KB."""
    from . import index_db
    from .embeddings.fusion import rrf_fuse
    store = _load_store()
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
            store.kb_dir, q, limit=limit, min_score=min_score,
        )
        used = "embedding" if hits else used
    if not hits and backend in ("auto", "fts5"):
        hits = index_db.search(store.kb_dir, q, limit=limit)
        used = "fts5" if hits else used
    if not hits and backend in ("auto", "substring"):
        hits = store.search_substring(q, limit=limit)
        used = "substring"
    if backend == "hybrid":
        emb = index_db.search_semantic(store.kb_dir, q, limit=limit * 2)
        fts = index_db.search(store.kb_dir, q, limit=limit * 2)
        hits = rrf_fuse(emb, fts, limit=limit)
        used = "hybrid"

    if rerank and hits:
        try:
            from .embeddings.rerank import default_reranker
            from .embeddings.rerank import rerank as do_rerank
            hits = do_rerank(query=query, hits=hits, reranker=default_reranker(),
                             top_k=limit)
        except ImportError:
            click.echo("warning: rerank extras not installed; skipping rerank",
                       err=True)

    if as_json:
        _emit_json({
            "backend": used,
            "hits": [
                {"kind": k, "id": i, "snippet": snip, "score": score,
                 "backend": used}
                for k, i, snip, score in hits
            ],
        })
        return

    label = _style(f"({used})", fg="green")
    for k, i, snip, score in hits:
        loc = _style(f"{k}/{i}", fg="cyan")
        if explain:
            sc = _style(f"score={score:.4f}", dim=True)
            _echo(f"{label} {loc}\t{sc}\t{snip}")
        else:
            _echo(f"{loc}\t{snip}  {label}")


@cli.command()
@click.argument("task")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--max-chars", default=None, type=int)
@click.option("--require-citations", is_flag=True)
@click.option("--min-items", default=0, type=int)
def context(task: str, limit: int, max_chars: int | None,
            require_citations: bool, min_items: int) -> None:
    """Build a ContextPack ready to inject into an agent prompt."""
    store = _load_store()
    pack = build_context_pack(
        store, query=task, limit=limit, max_chars=max_chars,
        min_items=min_items, require_citations=require_citations,
    )
    _emit_json(pack)


@cli.command()
def index() -> None:
    """Rebuild state.db from durable files."""
    store = _load_store()
    with _cli_errors():
        stats = health.rebuild_index(store, on_progress=_progress_cb("indexing"))
    click.echo(f"indexed: {stats}")


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
        click.echo(
            f"{r['kind']}/{r['id']} ~ {r['kind']}/{r['near_id']}  "
            f"cos={r['cosine']:.4f}"
        )


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
        rows = conn.execute(
            "SELECT kind, COUNT(*) FROM embedding_index GROUP BY kind"
        ).fetchall()
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
    canonical = tuple(
        "recall@k" if m.startswith("recall@") else m for m in metrics
    )
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


@cli.command()
@click.option("--embeddings/--no-embeddings", default=False,
              help="Rebuild the embedding index in addition to FTS5.")
@click.option("--backfill/--no-backfill", default=False,
              help="Re-encode every artifact under the current model.")
@click.option("--force/--no-force", default=False,
              help="Re-encode even if content hash unchanged.")
@click.option("--model", default=None,
              help="Adapter name; defaults to the registered default.")
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
def audit(tail: int, as_json: bool) -> None:
    """Read the audit log."""
    store = _load_store()
    events = list(audit_mod.read_events(store.kb_dir))[-tail:]
    if as_json:
        _emit_json([e.model_dump(mode="json") for e in events])
        return
    for e in events:
        click.echo(
            f"{e.created_at.isoformat()}  {e.event:30s}  by {e.actor}  "
            f"objects={e.object_ids}"
        )


# --- export / import ------------------------------------------------------


@cli.command()
@click.option("--out", "out_path", required=True, type=click.Path(dir_okay=False))
def export(out_path: str) -> None:
    """Bundle the durable KB into a portable .tar.gz."""
    store = _load_store()
    with _cli_errors():
        manifest = bundle.export(
            store.kb_dir, dest=Path(out_path), actor=_whoami(),
            on_progress=_progress_cb("exporting"),
        )
    _emit_json({
        "bundle_id": manifest["bundle_id"],
        "files": len(manifest["files"]),
        "out": out_path,
    })


@cli.command("export-check")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
def export_check_cmd(bundle_path: str) -> None:
    """Verify every file in a bundle matches its manifest hash."""
    r = bundle.export_check(Path(bundle_path))
    _emit_json({
        "ok": r.ok, "bundle_id": r.bundle_id,
        "files_checked": r.files_checked, "issues": r.issues,
    })
    sys.exit(0 if r.ok else 1)


@cli.command("import-check")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
def import_check_cmd(bundle_path: str) -> None:
    """Diff a bundle against the destination KB without writing."""
    store = _load_store()
    r = bundle.import_check(store.kb_dir, Path(bundle_path))
    _emit_json({
        "ok": r.ok, "bundle_id": r.bundle_id,
        "new_files": r.new_files, "conflicts": r.conflicts,
        "identical_files": len(r.identical), "issues": r.issues,
    })


@cli.command("import-apply")
@click.argument("bundle_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--on-conflict", default="skip", show_default=True,
              type=click.Choice(["skip", "overwrite", "fail"]))
def import_apply_cmd(bundle_path: str, on_conflict: str) -> None:
    """Apply a bundle. Default policy is skip — never destructive without explicit overwrite."""
    store = _load_store()
    try:
        r = bundle.import_apply(
            store.kb_dir, Path(bundle_path),
            on_conflict=on_conflict, actor=_whoami(),
            on_progress=_progress_cb("importing"),
        )
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    # Rebuild the index after a bulk import so search picks up new claims.
    health.rebuild_index(store, on_progress=_progress_cb("indexing"))
    _emit_json(r)


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
@click.option("--on-conflict", default="fail", show_default=True,
              type=click.Choice(["fail", "skip", "propose"]))
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
@click.argument("new_id")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit the diff as JSON.")
def diff(old_id: str, new_id: str, as_json: bool) -> None:
    """Show what changed between two claim or two page revisions."""
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
@click.option("--transport", default="stdio", show_default=True,
              type=click.Choice(["stdio", "jsonl", "http"]))
@click.option("--host", default="127.0.0.1", show_default=True,
              help="HTTP bind host (transport=http).")
@click.option("--port", default=None, type=int,
              help="HTTP bind port (transport=http; default 8731).")
@click.option("--token", default=None, envvar="VOUCH_HTTP_TOKEN",
              help="Bearer token for HTTP /rpc (or env VOUCH_HTTP_TOKEN). "
                   "Required to bind a non-loopback host.")
@click.option("--allow-public", is_flag=True,
              help="Permit binding a non-loopback host (requires --token).")
def serve(transport: str, host: str, port: int | None, token: str | None,
          allow_public: bool) -> None:
    """Run the MCP server (stdio), the JSONL tool server, or the HTTP server."""
    if transport == "stdio":
        from .server import run_stdio
        run_stdio()
    elif transport == "jsonl":
        from .jsonl_server import run_jsonl
        run_jsonl()
    else:
        from .http_server import DEFAULT_PORT, run_http
        bind_port = port if port is not None else DEFAULT_PORT
        try:
            run_http(host, bind_port, token=token, allow_public=allow_public)
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
@click.option("--state", type=click.Choice(["merged", "closed", "all"]), default="all",
              show_default=True, help="Which PR states to fetch.")
@click.option("--limit", type=int, default=200, show_default=True,
              help="Max PRs per state to fetch from gh.")
@click.option("--analyze-closed", is_flag=True,
              help="Run Claude/Anthropic to summarise WHY each closed-not-merged "
                   "PR was closed (uses local `claude` CLI if present, else "
                   "ANTHROPIC_API_KEY). Skipped silently when neither is set.")
@click.option("--reanalyze", is_flag=True,
              help="Re-run close-reason analysis even if a previous result is cached.")
@click.option("--analyzer", type=click.Choice(["auto", "claude-cli", "anthropic-api", "none"]),
              default="auto", show_default=True,
              help="Which close-reason analyzer to prefer.")
@click.option("--no-fetch-files", is_flag=True,
              help="Skip per-PR file-list fetch (faster, but dedup by file overlap stops working).")
@click.option("--cache-dir", default=None, type=click.Path(file_okay=False),
              help="Override cache directory (also env VOUCH_PR_CACHE_DIR).")
def pr_cache_build(repo: str, state: str, limit: int, analyze_closed: bool,
                   reanalyze: bool, analyzer: str, no_fetch_files: bool,
                   cache_dir: str | None) -> None:
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
    _emit_json({
        "repo": ref.slug,
        "fetched": result.fetched,
        "new": result.new,
        "updated": result.updated,
        "analyzed": result.analyzed,
        "skipped_analysis": result.skipped_analysis,
        "cache_path": str(result.path),
    })


@pr_cache_group.command("check")
@click.argument("repo")
@click.option("--topic", required=True,
              help="Short description of the PR you're about to raise (title-like text).")
@click.option("--files", default="",
              help="Comma-separated list of paths the planned PR would touch "
                   "(boosts dedup precision).")
@click.option("--min-score", default=0.15, show_default=True, type=float,
              help="Minimum similarity (0..1) for a cached PR to count as a duplicate signal.")
@click.option("--top-k", default=5, show_default=True, type=int)
@click.option("--cache-dir", default=None, type=click.Path(file_okay=False),
              help="Override cache directory (also env VOUCH_PR_CACHE_DIR).")
def pr_cache_check(repo: str, topic: str, files: str, min_score: float,
                   top_k: int, cache_dir: str | None) -> None:
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
    _emit_json({
        "repo": ref.slug,
        "cache_path": str(path),
        "cache_size": len(cache),
        "topic": topic,
        "files": file_list,
        "candidates": [c.as_json() for c in cands],
        # 0.7 (= 70 % of topic tokens contained in a cached PR's title+body)
        # is the threshold for "almost certainly the same idea." Below that,
        # surface as a soft signal the caller should eyeball before raising.
        "verdict": "likely_duplicate" if any(c.score >= 0.70 for c in cands)
        else "review_candidates" if cands
        else "no_match",
    })


@pr_cache_group.command("show")
@click.argument("repo")
@click.option("--state", type=click.Choice(["merged", "closed", "all"]), default="all",
              show_default=True)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
@click.option("--cache-dir", default=None, type=click.Path(file_okay=False),
              help="Override cache directory (also env VOUCH_PR_CACHE_DIR).")
def pr_cache_show(repo: str, state: str, limit: int, as_json: bool,
                  cache_dir: str | None) -> None:
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
        _emit_json({
            "repo": ref.slug,
            "cache_path": str(path),
            "count": len(records),
            "prs": [
                {
                    "number": r.number, "state": r.state, "title": r.title,
                    "url": r.url, "merged_at": r.merged_at, "closed_at": r.closed_at,
                    "files": r.files, "labels": r.labels,
                    "close_analysis": (
                        asdict(r.close_analysis) if r.close_analysis else None
                    ),
                }
                for r in records
            ],
        })
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


if __name__ == "__main__":
    cli()
