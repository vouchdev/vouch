"""`kb.timeline` — chronological entity trajectory (vouchdev/vouch#313).

Pins ordering (both axes), the since/until/types/limit filters, the
superseded-still-visible case, the four-site registration, and the read-only
invariant (a timeline run adds no audit mutation) against a fixture entity whose
history is known by construction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch.audit import count_events, log_event
from vouch.cli import cli
from vouch.models import (
    Claim,
    ClaimStatus,
    ClaimType,
    Entity,
    EntityType,
    Relation,
    RelationType,
)
from vouch.storage import KBStore
from vouch.timeline import TimelineError, build_timeline

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _seed(store: KBStore) -> str:
    """One entity `acme` with three claims (accrued t-3d, t-2d, t-1d, the last
    superseded) and one relation (t-2d). A second entity's claim is present to
    prove entity-scoping. Approve events land in the audit log for order=decided.
    """
    src = store.put_source(b"evidence")
    store.put_entity(Entity(id="acme", name="Acme", type=EntityType.PROJECT))
    store.put_entity(Entity(id="other", name="Other", type=EntityType.PROJECT))

    def claim(cid, days_ago, *, ctype=ClaimType.FACT, status=ClaimStatus.WORKING, ents=("acme",)):
        store.put_claim(Claim(
            id=cid, text=f"text {cid}", type=ctype, status=status,
            evidence=[src.id], entities=list(ents),
            created_at=NOW - timedelta(days=days_ago),
        ))

    claim("c1", 3, ctype=ClaimType.FACT)
    claim("c2", 2, ctype=ClaimType.DECISION)
    claim("c3", 1, ctype=ClaimType.FACT, status=ClaimStatus.SUPERSEDED)
    claim("c_other", 1, ents=("other",))  # different entity — must not appear

    store.put_relation(Relation(
        id="r1", source="acme", relation=RelationType.DEPENDS_ON, target="other",
        evidence=[src.id], created_at=NOW - timedelta(days=2),
    ))

    # approval events (order=decided): c1 approved LAST despite earliest created.
    def approve(pid, rid, days_ago):
        log_event(store.kb_dir, event="proposal.claim.approve", actor="rev",
                  object_ids=[pid, rid])
        path = store.kb_dir / "audit.log.jsonl"
        lines = path.read_text().splitlines()
        obj = json.loads(lines[-1])
        obj["created_at"] = (NOW - timedelta(days=days_ago)).isoformat()
        lines[-1] = json.dumps(obj, separators=(",", ":"), sort_keys=True)
        path.write_text("\n".join(lines) + "\n")

    approve("p3", "c3", 5)   # c3: created t-1d but decided t-5d (earliest)
    approve("p2", "c2", 4)
    approve("p1", "c1", 3)   # c1: created t-3d but decided t-3d (latest)
    return "acme"


# --- ordering -------------------------------------------------------------


def test_effective_order_by_created_at(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme", order="effective")
    ids = [e["id"] for e in tl["entries"]]
    # created order: c1(t-3d), then c2 & r1 (both t-2d, id tie-break), then c3(t-1d)
    assert ids == ["c1", "c2", "r1", "c3"]
    assert tl["count"] == 4
    assert "c_other" not in ids  # entity-scoped


def test_decided_order_from_audit(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme", order="decided")
    ids = [e["id"] for e in tl["entries"]]
    # decided order: c3(t-5d), c2(t-4d), c1(t-3d); r1 has no approve event ->
    # falls back to its created_at (t-2d), so it sorts last.
    assert ids == ["c3", "c2", "c1", "r1"]


# --- filters --------------------------------------------------------------


def test_types_filter_claim_type(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme", types=["decision"])
    assert [e["id"] for e in tl["entries"]] == ["c2"]


def test_types_filter_relation_literal(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme", types=["relation"])
    assert [e["id"] for e in tl["entries"]] == ["r1"]
    assert tl["entries"][0]["status"] is None  # relations carry no status


def test_since_until_window(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(
        store, "acme",
        since=NOW - timedelta(days=2, hours=1),
        until=NOW - timedelta(hours=1),
    )
    # only t-2d (c2, r1) and t-1d (c3) fall in the window
    assert [e["id"] for e in tl["entries"]] == ["c2", "r1", "c3"]


def test_limit_keeps_most_recent(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme", limit=2)
    assert [e["id"] for e in tl["entries"]] == ["r1", "c3"]  # newest two, chronological
    assert tl["count"] == 2
    assert tl["total"] == 4


# --- status visibility ----------------------------------------------------


def test_superseded_still_visible_flagged(store: KBStore) -> None:
    _seed(store)
    tl = build_timeline(store, "acme")
    c3 = next(e for e in tl["entries"] if e["id"] == "c3")
    assert c3["status"] == "superseded"  # retired but still shown


# --- errors ---------------------------------------------------------------


def test_missing_entity_raises(store: KBStore) -> None:
    from vouch.storage import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError):
        build_timeline(store, "nope")


def test_bad_order_raises(store: KBStore) -> None:
    _seed(store)
    with pytest.raises(TimelineError):
        build_timeline(store, "acme", order="sideways")


# --- read-only invariant --------------------------------------------------


def test_timeline_writes_nothing(store: KBStore) -> None:
    _seed(store)
    before = count_events(store.kb_dir)
    build_timeline(store, "acme", order="decided")
    build_timeline(store, "acme", order="effective", limit=1)
    assert count_events(store.kb_dir) == before  # no mutation event appended


# --- four-site registration ----------------------------------------------


def test_registered_at_all_sites() -> None:
    from vouch.capabilities import capabilities
    from vouch.jsonl_server import HANDLERS
    from vouch.server import kb_timeline  # noqa: F401  (mcp tool exists)

    assert "kb.timeline" in set(capabilities().methods)
    assert "kb.timeline" in HANDLERS


def test_jsonl_handler_runs(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    from vouch.jsonl_server import HANDLERS

    out = HANDLERS["kb.timeline"]({"entity_id": "acme", "order": "effective"})
    assert [e["id"] for e in out["entries"]] == ["c1", "c2", "r1", "c3"]


# --- cli ------------------------------------------------------------------


def test_cli_table(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["timeline", "acme"])
    assert res.exit_code == 0, res.output
    assert "timeline: Acme" in res.output
    assert "c1" in res.output and "r1" in res.output


def test_cli_json(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["timeline", "acme", "--json", "--order", "decided"])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["order"] == "decided"
    assert [e["id"] for e in doc["entries"]] == ["c3", "c2", "c1", "r1"]


def test_cli_missing_entity(store: KBStore, monkeypatch) -> None:
    _seed(store)
    monkeypatch.chdir(store.root)
    res = CliRunner().invoke(cli, ["timeline", "ghost"])
    assert res.exit_code != 0
    assert "not found" in res.output.lower()
