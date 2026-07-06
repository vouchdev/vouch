"""Session lifecycle — start, end, crystallize.

A Session bundles every proposal an agent filed during one work block.
`crystallize` is the convenience operation that approves every still-pending
proposal in a session and (optionally) writes a session-summary Page.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime

from . import audit, index_db, volunteer_context
from . import salience as salience_mod
from .models import Page, PageType, ProposalKind, ProposalStatus, Session
from .proposals import approve
from .storage import ArtifactNotFoundError, KBStore

logger = logging.getLogger(__name__)


def new_session_id() -> str:
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"sess-{ts}-{uuid.uuid4().hex[:6]}"


def session_start(store: KBStore, *, agent: str, task: str | None = None,
                  note: str | None = None) -> Session:
    sess = Session(id=new_session_id(), agent=agent, task=task, note=note)
    store.put_session(sess)
    audit.log_event(
        store.kb_dir, event="session.start", actor=agent,
        object_ids=[sess.id], data={"task": task},
    )
    volunteer_context.on_session_start(store, sess)
    return sess


def session_end(store: KBStore, session_id: str, *, note: str | None = None) -> Session:
    sess = store.get_session(session_id)
    salience_mod.reset_session(session_id)
    if sess.ended_at is not None:
        return sess  # idempotent
    sess.ended_at = datetime.now(UTC)
    if note:
        sess.note = (sess.note + "\n" if sess.note else "") + note
    # Backfill proposal_ids by scanning proposals whose session_id matches.
    sess.proposal_ids = sorted({
        p.id for p in store.list_proposals() if p.session_id == sess.id
    })
    store.update_session(sess)
    audit.log_event(
        store.kb_dir, event="session.end", actor=sess.agent,
        object_ids=[sess.id], data={"proposals": len(sess.proposal_ids)},
    )
    volunteer_context.on_session_end(session_id)
    return sess


def crystallize(
    store: KBStore,
    session_id: str,
    *,
    approver: str,
    write_summary_page: bool = True,
) -> dict:
    """Approve every still-pending proposal in `session_id`.

    Optionally also creates a session-summary Page that links to the
    approved claims, so the next session has a single readable entry point.
    Returns counts + ids of what happened.
    """
    sess = store.get_session(session_id)
    pending = [
        p for p in store.list_proposals(ProposalStatus.PENDING)
        if p.session_id == sess.id
    ]
    approved_artifact_ids: list[str] = []
    failures: list[dict] = []
    for proposal in pending:
        try:
            artifact = approve(
                store, proposal.id,
                approved_by=approver,
                reason=f"crystallized via session {sess.id}",
            )
            approved_artifact_ids.append(artifact.id)
        except Exception as e:
            logger.exception(
                "crystallize: approve(%s) failed in session %s",
                proposal.id, sess.id,
            )
            failures.append({
                "proposal_id": proposal.id,
                "error": str(e),
                "error_type": type(e).__name__,
            })

    summary_page_id: str | None = None
    if write_summary_page and approved_artifact_ids:
        page = Page(
            id=f"session-{sess.id}",
            title=f"Session {sess.id}",
            type=PageType.SESSION,
            body=_build_summary_body(sess, approved_artifact_ids),
            claims=[
                aid for aid in approved_artifact_ids
                if (store.kb_dir / "claims" / f"{aid}.yaml").exists()
            ],
        )
        store.put_page(page)
        with index_db.open_db(store.kb_dir) as conn:
            index_db.index_page(
                conn, id=page.id, title=page.title, body=page.body,
                type=page.type, tags=page.tags,
            )
        summary_page_id = page.id

    crystallize_object_ids = [sess.id, *approved_artifact_ids]
    if summary_page_id is not None:
        crystallize_object_ids.append(summary_page_id)
    audit.log_event(
        store.kb_dir, event="session.crystallize", actor=approver,
        object_ids=crystallize_object_ids,
        data={"approved": len(approved_artifact_ids), "failed": len(failures)},
    )
    return {
        "session_id": sess.id,
        "approved": approved_artifact_ids,
        "failures": failures,
        "summary_page_id": summary_page_id,
    }


def build_start_context(store: KBStore, ref: str) -> dict[str, str]:
    """Seed context for starting a new agent session from a previous one.

    ``ref`` points at a session summary or claim: a *proposal* id (pending
    or decided — captured summaries live here before review), an approved
    *claim* id (captured sessions land as claims; any other approved claim
    works as a seed too), or an approved *page* id (pre-claim captures).
    Returns ``{ref, title, status, seed}`` where ``seed`` is a paste-ready
    context block for a fresh agent session, e.g.::

        claude "$(vouch session start-from <ref>)"

    Read-only — nothing is written and no proposal status changes.
    """
    title = body = status = ""
    try:
        proposal = store.get_proposal(ref)
    except ArtifactNotFoundError:
        proposal = None
    if proposal is not None:
        if proposal.kind == ProposalKind.CLAIM:
            body = str(proposal.payload.get("text") or "")
            title = (
                body.lstrip().splitlines()[0].lstrip("# ").strip()
                if body.strip() else ref
            )
        elif proposal.kind == ProposalKind.PAGE:
            title = str(proposal.payload.get("title") or ref)
            body = str(proposal.payload.get("body") or "")
        else:
            raise ValueError(
                f"{ref} is a {proposal.kind.value} proposal; "
                "start-from needs a session summary claim or page"
            )
        status = f"{proposal.status.value} proposal"
    else:
        claim = page = None
        try:
            claim = store.get_claim(ref)
        except ArtifactNotFoundError:
            try:
                page = store.get_page(ref)
            except ArtifactNotFoundError as e:
                raise ArtifactNotFoundError(
                    f"no session summary found for {ref!r} "
                    "(tried proposal, approved claim, and approved page ids)"
                ) from e
        if claim is not None:
            body = claim.text
            title = (
                body.lstrip().splitlines()[0].lstrip("# ").strip() or ref
            )
            status = "approved claim"
        else:
            assert page is not None
            title = page.title
            body = page.body
            status = "approved page"
    seed = (
        "you are starting a new session seeded from a previous session's "
        f"summary (vouch {status} `{ref}`). continue where that session left "
        "off: treat the record below as reviewed context, verify anything "
        "that may have gone stale against the current repo state, and recall "
        "more with kb_context or /vouch-recall before re-deriving.\n\n"
        "---\n\n"
        f"{body.strip()}\n"
    )
    return {"ref": ref, "title": title, "status": status, "seed": seed}


def _build_summary_body(sess: Session, ids: list[str]) -> str:
    # The summary page is durable and surfaces in kb.read_page / kb.search /
    # kb.context, but never goes through propose_page + approve. The body is
    # therefore restricted to fields the proposing agent cannot influence —
    # session id (server-generated), timestamps (set from server clock at
    # session_start / session_end), and the list of artifact ids that did go
    # through the review gate. Anything agent-controlled (sess.task,
    # sess.note, sess.agent) is omitted to keep the review-gate guarantee
    # intact for the Page artifact kind. See #76.
    lines = [
        f"# Session {sess.id}",
        "",
        f"**Started:** {sess.started_at.isoformat()}",
        f"**Ended:** {(sess.ended_at or datetime.now(UTC)).isoformat()}",
        "",
        "## Crystallized artifacts",
        "",
    ]
    for aid in ids:
        lines.append(f"- `{aid}`")
    return "\n".join(lines)
