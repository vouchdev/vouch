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
from pathlib import Path

import click
import yaml

from . import __version__, bundle, health
from . import audit as audit_mod
from . import lifecycle as life
from . import sessions as sess_mod
from . import verify as verify_mod
from .capabilities import capabilities as build_caps
from .context import build_context_pack
from .models import ProposalStatus
from .proposals import (
    approve as do_approve,
)
from .proposals import (
    propose_claim,
    propose_entity,
    propose_page,
    propose_relation,
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


def _load_store(start: Path | None = None) -> KBStore:
    try:
        return KBStore(discover_root(start))
    except KBNotFoundError as e:
        click.echo(f"error: {e}", err=True)
        click.echo("hint: run `vouch init` in your project root.", err=True)
        sys.exit(2)


def _whoami() -> str:
    return os.environ.get("VOUCH_USER") or getpass.getuser()


def _emit_json(obj) -> None:
    click.echo(json.dumps(obj, indent=2, default=str, sort_keys=True))


@click.group()
@click.version_option(__version__, prog_name="vouch")
def cli() -> None:
    """vouch — git-native, review-gated knowledge base for LLM agents."""


# --- bootstrap ------------------------------------------------------------


@cli.command()
@click.option("--path", default=".", type=click.Path(file_okay=False), show_default=True)
def init(path: str) -> None:
    """Initialise a .vouch/ knowledge base at PATH."""
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = KBStore.init(root)
    audit_mod.log_event(store.kb_dir, event="kb.init", actor=_whoami())
    click.echo(f"Initialised KB at {store.kb_dir}")
    click.echo("Next: `vouch serve` to expose the MCP server to your agent.")


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
    click.echo(f"  pending: {s['pending_proposals']} proposals")
    click.echo(f"  audit:   {s['audit_events']} events  •  "
               f"index: {'present' if s['index_present'] else 'missing'}")


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


# --- proposals ------------------------------------------------------------


@cli.command()
def pending() -> None:
    """List proposals awaiting review."""
    store = _load_store()
    pending = store.list_proposals(ProposalStatus.PENDING)
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


@cli.command()
@click.argument("proposal_id")
def show(proposal_id: str) -> None:
    """Show full details of a proposal."""
    store = _load_store()
    try:
        pr = store.get_proposal(proposal_id)
    except ArtifactNotFoundError as e:
        raise click.ClickException(str(e)) from e
    click.echo(yaml.safe_dump(pr.model_dump(mode="json"), sort_keys=False))


@cli.command()
@click.argument("proposal_id")
@click.option("--reason", default=None)
def approve(proposal_id: str, reason: str | None) -> None:
    """Approve a proposal — converts it into a durable artifact."""
    store = _load_store()
    try:
        artifact = do_approve(store, proposal_id, approved_by=_whoami(), reason=reason)
    except (ArtifactNotFoundError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Approved → {type(artifact).__name__.lower()}/{artifact.id}")


@cli.command()
@click.argument("proposal_id")
@click.option("--reason", required=True)
def reject(proposal_id: str, reason: str) -> None:
    """Reject a proposal — recorded for audit and future agent context."""
    store = _load_store()
    try:
        do_reject(store, proposal_id, rejected_by=_whoami(), reason=reason)
    except (ArtifactNotFoundError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Rejected {proposal_id}")


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
    life.supersede(store, old_claim_id=old_claim_id,
                   new_claim_id=new_claim_id, actor=_whoami())
    click.echo(f"superseded {old_claim_id} -> {new_claim_id}")


@cli.command()
@click.argument("claim_a")
@click.argument("claim_b")
def contradict(claim_a: str, claim_b: str) -> None:
    """Record that two claims contradict each other."""
    store = _load_store()
    life.contradict(store, claim_a=claim_a, claim_b=claim_b, actor=_whoami())
    click.echo(f"contradiction recorded: {claim_a} <-> {claim_b}")


@cli.command()
@click.argument("claim_id")
def archive(claim_id: str) -> None:
    """Archive a claim (kept for history, omitted from default retrieval)."""
    store = _load_store()
    life.archive(store, claim_id=claim_id, actor=_whoami())
    click.echo(f"archived {claim_id}")


@cli.command()
@click.argument("claim_id")
def confirm(claim_id: str) -> None:
    """Re-confirm a claim — bumps last_confirmed_at."""
    store = _load_store()
    life.confirm(store, claim_id=claim_id, actor=_whoami())
    click.echo(f"confirmed {claim_id}")


@cli.command()
@click.argument("claim_id")
def cite(claim_id: str) -> None:
    """Resolve and print all citations backing a claim."""
    store = _load_store()
    out = []
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
    sess = sess_mod.session_end(store, session_id, note=note)
    _emit_json({"session": sess.id, "proposals": sess.proposal_ids})


@cli.command()
@click.argument("session_id")
@click.option("--no-page", is_flag=True, help="Skip the session-summary page.")
def crystallize(session_id: str, no_page: bool) -> None:
    """Approve every pending proposal in a session (and write a summary page)."""
    store = _load_store()
    result = sess_mod.crystallize(
        store, session_id, approver=_whoami(), write_summary_page=not no_page,
    )
    _emit_json(result)


# --- retrieval ------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("--limit", default=10, show_default=True, type=int)
def search(query: str, limit: int) -> None:
    """FTS5 search over claims, pages, and entities."""
    from . import index_db
    store = _load_store()
    try:
        hits = index_db.search(store.kb_dir, query, limit=limit)
        if not hits:
            hits = store.search_substring(query, limit=limit)
            backend = "substring"
        else:
            backend = "fts5"
    except Exception:
        hits = store.search_substring(query, limit=limit)
        backend = "substring"
    for kind, hid, snippet, score in hits:
        click.echo(f"[{kind}] {hid}  score={score:.3f}  ({backend})")
        click.echo(f"    {snippet[:200]}")


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
    _emit_json(pack.model_dump(mode="json"))


@cli.command()
def index() -> None:
    """Rebuild state.db from durable files."""
    store = _load_store()
    stats = health.rebuild_index(store)
    click.echo(f"indexed: {stats}")


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
    manifest = bundle.export(store.kb_dir, dest=Path(out_path), actor=_whoami())
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
        )
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e
    # Rebuild the index after a bulk import so search picks up new claims.
    health.rebuild_index(store)
    _emit_json(r)


# --- serve ----------------------------------------------------------------


@cli.command()
@click.option("--transport", default="stdio", show_default=True,
              type=click.Choice(["stdio", "jsonl"]))
def serve(transport: str) -> None:
    """Run the MCP server (stdio) or the JSONL tool server."""
    if transport == "stdio":
        from .server import run_stdio
        run_stdio()
    else:
        from .jsonl_server import run_jsonl
        run_jsonl()


if __name__ == "__main__":
    cli()
