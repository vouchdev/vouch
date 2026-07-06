"""Claim/page revision diff — `vouch diff`."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from vouch import audit
from vouch.capabilities import capabilities
from vouch.cli import cli
from vouch.diff import ArtifactDiff, DiffError, diff_artifacts
from vouch.jsonl_server import HANDLERS, handle_request
from vouch.models import Claim, ClaimStatus, Page
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def _claim(store: KBStore, cid: str, **kw: object) -> Claim:
    src = store.put_source(b"e")
    fields = {"id": cid, "text": "t", "evidence": [src.id]}
    fields.update(kw)
    return store.put_claim(Claim(**fields))  # type: ignore[arg-type]


# --- diff_artifacts -------------------------------------------------------


def test_diff_claims_reports_changed_scalar_fields(store: KBStore) -> None:
    _claim(store, "c1", status=ClaimStatus.WORKING, confidence=0.7)
    _claim(store, "c2", status=ClaimStatus.STABLE, confidence=0.9)
    d = diff_artifacts(store, "c1", "c2")
    assert isinstance(d, ArtifactDiff)
    assert d.kind == "claim"
    changed = {c.field: (c.old, c.new) for c in d.changes}
    assert changed["status"] == ("working", "stable")
    assert changed["confidence"] == (0.7, 0.9)


def test_diff_claims_text_change_produces_line_diff(store: KBStore) -> None:
    _claim(store, "c1", text="the old wording")
    _claim(store, "c2", text="the new wording")
    d = diff_artifacts(store, "c1", "c2")
    assert any(line.startswith("-the old wording") for line in d.text_diff)
    assert any(line.startswith("+the new wording") for line in d.text_diff)
    # text is rendered as a diff, not as a scalar FieldChange
    assert "text" not in {c.field for c in d.changes}


def test_diff_identical_claims_has_no_changes(store: KBStore) -> None:
    _claim(store, "c1", text="same", status=ClaimStatus.STABLE)
    _claim(store, "c2", text="same", status=ClaimStatus.STABLE)
    d = diff_artifacts(store, "c1", "c2")
    assert d.changes == []
    assert d.text_diff == []


def test_diff_pages_reports_title_and_body(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="Old", body="line one"))
    store.put_page(Page(id="p2", title="New", body="line two"))
    d = diff_artifacts(store, "p1", "p2")
    assert d.kind == "page"
    changed = {c.field: (c.old, c.new) for c in d.changes}
    assert changed["title"] == ("Old", "New")
    assert any(line.startswith("+line two") for line in d.text_diff)


def test_diff_unknown_id_raises(store: KBStore) -> None:
    _claim(store, "c1")
    with pytest.raises(DiffError, match="unknown artifact: nope"):
        diff_artifacts(store, "c1", "nope")


def test_diff_mismatched_kinds_raises(store: KBStore) -> None:
    _claim(store, "c1")
    store.put_page(Page(id="p1", title="P", body="b"))
    with pytest.raises(DiffError, match="cannot diff"):
        diff_artifacts(store, "c1", "p1")


def test_diff_omitted_new_id_resolves_via_superseded_by(store: KBStore) -> None:
    _claim(store, "c2", text="new wording")
    _claim(store, "c1", text="old wording", superseded_by="c2")
    d = diff_artifacts(store, "c1")
    assert d.new_id == "c2"
    assert any(line.startswith("+new wording") for line in d.text_diff)


def test_diff_omitted_new_id_without_successor_raises(store: KBStore) -> None:
    _claim(store, "c1")
    with pytest.raises(DiffError, match="has not been superseded"):
        diff_artifacts(store, "c1")


def test_diff_omitted_new_id_for_page_raises(store: KBStore) -> None:
    store.put_page(Page(id="p1", title="P", body="b"))
    with pytest.raises(DiffError, match="pages have no successor pointer"):
        diff_artifacts(store, "p1")


def test_diff_read_only_writes_no_audit_event_or_proposal(store: KBStore) -> None:
    _claim(store, "c1", text="old")
    _claim(store, "c2", text="new")
    before = list(audit.read_events(store.kb_dir))
    diff_artifacts(store, "c1", "c2")
    after = list(audit.read_events(store.kb_dir))
    assert after == before
    assert store.list_proposals() == []


# --- CLI ------------------------------------------------------------------


def test_cli_diff_prints_changed_fields(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", text="old", status=ClaimStatus.WORKING)
    _claim(store, "c2", text="new", status=ClaimStatus.STABLE)
    res = CliRunner().invoke(cli, ["diff", "c1", "c2"])
    assert res.exit_code == 0, res.output
    assert "status: working" in res.output and "stable" in res.output
    assert "-old" in res.output and "+new" in res.output


def test_cli_diff_json(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", status=ClaimStatus.WORKING)
    _claim(store, "c2", status=ClaimStatus.STABLE)
    res = CliRunner().invoke(cli, ["diff", "c1", "c2", "--json"])
    assert res.exit_code == 0, res.output
    import json
    payload = json.loads(res.output)
    assert payload["kind"] == "claim"
    assert any(c["field"] == "status" for c in payload["changes"])


def test_cli_diff_identical_says_no_differences(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", text="same")
    _claim(store, "c2", text="same")
    res = CliRunner().invoke(cli, ["diff", "c1", "c2"])
    assert res.exit_code == 0, res.output
    assert "no differences" in res.output


def test_cli_diff_unknown_id_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    res = CliRunner().invoke(cli, ["diff", "c1", "nope"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "unknown artifact: nope" in res.output


def test_cli_diff_omitted_new_id_resolves_successor(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c2", text="new")
    _claim(store, "c1", text="old", superseded_by="c2")
    res = CliRunner().invoke(cli, ["diff", "c1"])
    assert res.exit_code == 0, res.output
    assert "diff claim c1 → c2" in res.output


def test_cli_diff_omitted_new_id_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    res = CliRunner().invoke(cli, ["diff", "c1"])
    assert res.exit_code != 0
    assert "Traceback" not in res.output
    assert "has not been superseded" in res.output


# --- kb.* RPC surface -------------------------------------------------------


def test_diff_method_in_capabilities() -> None:
    methods = set(capabilities().methods)
    assert "kb.diff" in methods
    assert set(capabilities().methods) == set(HANDLERS.keys())


def test_kb_diff_over_jsonl(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1", status=ClaimStatus.WORKING)
    _claim(store, "c2", status=ClaimStatus.STABLE)
    resp = handle_request(
        {"id": "1", "method": "kb.diff", "params": {"old_id": "c1", "new_id": "c2"}}
    )
    assert resp["ok"] is True, resp
    assert resp["result"]["kind"] == "claim"
    changed = {c["field"]: (c["old"], c["new"]) for c in resp["result"]["changes"]}
    assert changed["status"] == ("working", "stable")


def test_kb_diff_missing_param_over_jsonl(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    resp = handle_request({"id": "2", "method": "kb.diff", "params": {}})
    assert resp["ok"] is False
    assert resp["error"]["code"] == "missing_param"


def test_kb_diff_unknown_id_over_jsonl(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    _claim(store, "c1")
    resp = handle_request(
        {"id": "3", "method": "kb.diff", "params": {"old_id": "c1", "new_id": "nope"}}
    )
    assert resp["ok"] is False
    assert resp["error"]["code"] == "invalid_request"
