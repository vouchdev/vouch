"""Source verification — drift detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from vouch import verify
from vouch.models import Source
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


# --- security: verify_source must not read off-project locators ---------
#
# `Source.locator` accepts an arbitrary string (set verbatim from the
# `url` arg in `kb.register_source` on both MCP and JSONL transports, and
# from any bundle's meta.yaml). The previous implementation did
# `Path(source.locator).read_bytes()` for type=file sources with no
# containment check — a file-existence + hash-confirmation primitive
# triggered by `vouch source verify` / `vouch doctor`. These regressions
# pin the read through `KBStore.read_under_root`, which already enforces
# `is_relative_to(root)` + `O_NOFOLLOW` + `fstat S_ISREG`.


def test_verify_refuses_to_read_off_project_locator(
    store: KBStore, tmp_path: Path,
) -> None:
    """A source whose `locator` points outside the project root must be
    reported as `external_status="missing"` rather than `match` — proves
    the read never happened. Models the bundle-imported /
    `kb.register_source(url=...)` attack.

    Detection strategy: register a Source whose `id` is the sha256 of
    bytes we plant in an off-tree file. If the buggy `Path(locator)
    .read_bytes()` path is still alive, `verify_source` would read those
    bytes, hash them, get `external_status="match"`, and effectively
    confirm the off-tree file's content. After the fix,
    `read_under_root` refuses the read on containment, and the result
    is `missing`."""
    off_tree_root = tmp_path.parent / "off-tree-victim"
    off_tree_root.mkdir(exist_ok=True)
    victim = off_tree_root / "secret.txt"
    try:
        leaked = b"would-be-leaked-bytes"
        victim.write_bytes(leaked)
        # Source.id == sha256(leaked) so the legacy code path would
        # report `match` if it actually read the off-tree file.
        src = store.put_source(
            leaked, title="lying", locator=str(victim.resolve()),
            source_type="file",
        )
        assert src.id == hashlib.sha256(leaked).hexdigest()

        result = verify.verify_source(store, src)
        assert result.stored_ok is True
        # The key invariant: NOT "match". A buggy read of the off-tree
        # file would have returned `match` here.
        assert result.external_status == "missing", (
            f"verify_source read off-tree locator and reported "
            f"external_status={result.external_status!r} — "
            f"read_under_root containment guard regressed"
        )
        assert result.note is not None
        assert (
            "must be inside project root" in result.note
            or "unreadable" in result.note
        )
    finally:
        if victim.exists():
            victim.unlink()
        if off_tree_root.exists() and not any(off_tree_root.iterdir()):
            off_tree_root.rmdir()


def test_verify_refuses_to_read_absolute_system_path(
    store: KBStore, tmp_path: Path,
) -> None:
    """A `kb.register_source(url="/etc/passwd", source_type="file")`-style
    attack must not produce a hash-confirmation oracle on the system
    path. We don't depend on /etc/passwd existing — `read_under_root`
    rejects on containment before the open(), so the result is the same
    on Linux, macOS, and Windows."""
    src = store.put_source(
        b"placeholder", title="lying",
        locator="/etc/passwd" if sys_is_posix() else r"C:\Windows\win.ini",
        source_type="file",
    )
    result = verify.verify_source(store, src)
    assert result.external_status == "missing"
    assert result.note is not None  # containment / unreadable note attached


def sys_is_posix() -> bool:
    import os
    return os.name == "posix"


def test_verify_in_project_locator_still_detects_drift(
    store: KBStore, tmp_path: Path,
) -> None:
    """Positive: an in-project locator continues to be read (and drift
    is detected). Guards against the containment fix breaking honest
    `register_source_from_path` flows whose locators are scoped to the
    project root by design (#28)."""
    f = tmp_path / "doc.txt"
    f.write_bytes(b"original")
    src = store.put_source(
        f.read_bytes(), title="doc", locator=str(f.resolve()),
    )
    # Drift: overwrite + re-verify.
    f.write_bytes(b"changed")
    result = verify.verify_source(store, src)
    assert result.stored_ok is True
    assert result.external_status == "drift"


def test_verify_empty_locator_caught_by_model(store: KBStore) -> None:
    """`Source._locator_non_empty` rejects empty / whitespace-only
    locators at construction time, so an on-disk `locator: ""` from a
    pre-fix meta.yaml never reaches `verify_source` in the first place
    (model_validate at read time will surface it as an invalid Source)."""
    with pytest.raises(ValidationError, match="locator must be a non-empty"):
        Source(id="a" * 64, locator="")
    with pytest.raises(ValidationError, match="locator must be a non-empty"):
        Source(id="a" * 64, locator="   ")
