"""Source verification — drift detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from vouch import verify
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path)


def test_verify_detects_external_drift(store: KBStore, tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_bytes(b"original")
    src = store.put_source(
        f.read_bytes(), title="doc",
        locator=str(f.resolve()),
    )
    # Now overwrite the external file.
    f.write_bytes(b"changed")
    results = verify.verify_all(store)
    target = next(r for r in results if r.source.id == src.id)
    assert target.stored_ok  # stored copy is still fine
    assert target.external_status == "drift"


def test_verify_handles_missing_stored_content(store: KBStore, tmp_path: Path) -> None:
    # Regression for #30: verify_source caught FileNotFoundError, but
    # store.read_source_content() raises ArtifactNotFoundError (a KeyError
    # subclass) when the content file is missing. The mismatch crashed the
    # entire verification sweep when even one source had missing content.
    f = tmp_path / "doc.txt"
    f.write_bytes(b"will-be-orphaned")
    src = store.put_source(f.read_bytes(), title="doc", locator=str(f.resolve()))
    # Simulate the on-disk state the bug report describes: meta.yaml present,
    # content blob gone (e.g. partial clone, manual cleanup, disk error).
    (store.kb_dir / "sources" / src.id / "content").unlink()
    results = verify.verify_all(store)
    target = next(r for r in results if r.source.id == src.id)
    assert target.stored_ok is False
    assert target.external_status == "n/a"
    assert target.note == "stored content missing"


def test_verify_handles_unreadable_stored_content(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sibling regression for #30: read_source_content() can also raise OSError
    # (permission denied, TOCTOU between exists() and read_bytes(), disk I/O
    # error). Before the fix only ArtifactNotFoundError was caught, so any of
    # those still aborted verify_all(). Monkeypatch to make read_source_content
    # raise PermissionError so the test is portable across CI users / filesystems.
    f = tmp_path / "doc.txt"
    f.write_bytes(b"original")
    src = store.put_source(f.read_bytes(), title="doc", locator=str(f.resolve()))

    def _boom(_sid: str) -> bytes:
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(store, "read_source_content", _boom)
    results = verify.verify_all(store)
    target = next(r for r in results if r.source.id == src.id)
    assert target.stored_ok is False
    assert target.external_status == "n/a"
    assert target.note is not None
    assert "unreadable" in target.note
    assert "simulated permission denied" in target.note


def test_verify_all_continues_after_one_failure(
    store: KBStore, tmp_path: Path
) -> None:
    # End-to-end check that one broken source no longer takes down the sweep:
    # register two sources, break one, ensure verify_all returns both results.
    good = tmp_path / "good.txt"
    good.write_bytes(b"good")
    good_src = store.put_source(good.read_bytes(), title="good",
                                locator=str(good.resolve()))
    bad = tmp_path / "bad.txt"
    bad.write_bytes(b"bad")
    bad_src = store.put_source(bad.read_bytes(), title="bad",
                               locator=str(bad.resolve()))
    (store.kb_dir / "sources" / bad_src.id / "content").unlink()

    results = verify.verify_all(store)
    by_id = {r.source.id: r for r in results}
    assert by_id[good_src.id].stored_ok is True
    assert by_id[bad_src.id].stored_ok is False
    assert by_id[bad_src.id].note == "stored content missing"
