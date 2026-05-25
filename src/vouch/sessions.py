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

from . import audit, index_db
from .models import Page, PageType, ProposalStatus, Session
from .proposals import approve
from .storage import KBStore

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
    return sess


def session_end(store: KBStore, session_id: str, *, note: str | None = None) -> Session:
    sess = store.get_session(session_id)
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
                type=page.type.value, tags=page.tags,
            )
        summary_page_id = page.id

    audit.log_event(
        store.kb_dir, event="session.crystallize", actor=approver,
        object_ids=[sess.id, *approved_artifact_ids],
        data={"approved": len(approved_artifact_ids), "failed": len(failures)},
    )
    return {
        "session_id": sess.id,
        "approved": approved_artifact_ids,
        "failures": failures,
        "summary_page_id": summary_page_id,
    }


def _build_summary_body(sess: Session, ids: list[str]) -> str:
    lines = [f"# Session {sess.id}", ""]
    if sess.task:
        lines += [f"**Task:** {sess.task}", ""]
    lines += [
        f"**Agent:** {sess.agent}",
        f"**Started:** {sess.started_at.isoformat()}",
        f"**Ended:** {(sess.ended_at or datetime.now(UTC)).isoformat()}",
        "",
        "## Crystallized artifacts",
        "",
    ]
    for aid in ids:
        lines.append(f"- `{aid}`")
    return "\n".join(lines)
