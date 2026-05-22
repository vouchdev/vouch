"""Source verification — detect drift.

Re-hashes stored source content and compares to the recorded `hash` field.
For sources whose `locator` points to an external file, the *external file*
is also re-hashed to detect upstream changes; mismatches surface as
warnings so the agent / human can re-evaluate the claims that cite it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import audit
from .models import Source
from .storage import ArtifactNotFoundError, KBStore, sha256_hex


@dataclass
class VerificationResult:
    source: Source
    stored_ok: bool
    external_status: str  # "match" | "drift" | "missing" | "n/a"
    note: str | None = None


def verify_source(store: KBStore, source: Source) -> VerificationResult:
    try:
        body = store.read_source_content(source.id)
    except ArtifactNotFoundError:
        return VerificationResult(source=source, stored_ok=False,
                                  external_status="n/a", note="stored content missing")
    except OSError as e:
        # Permission denied, TOCTOU race between exists() and read_bytes(),
        # I/O error on the underlying filesystem — any of these should surface
        # as a graceful per-source failure rather than aborting verify_all().
        return VerificationResult(source=source, stored_ok=False,
                                  external_status="n/a",
                                  note=f"stored content unreadable: {e}")
    stored_ok = sha256_hex(body) == source.id

    external_status = "n/a"
    note: str | None = None
    if source.type.value == "file":
        ext = Path(source.locator)
        if ext.is_file():
            try:
                external_status = (
                    "match" if sha256_hex(ext.read_bytes()) == source.id else "drift"
                )
            except OSError as e:
                external_status = "missing"
                note = f"unreadable: {e}"
        else:
            external_status = "missing"
            note = "external file path no longer exists"

    return VerificationResult(
        source=source, stored_ok=stored_ok,
        external_status=external_status, note=note,
    )


def verify_all(store: KBStore, *, actor: str = "vouch-verify"
               ) -> list[VerificationResult]:
    results = [verify_source(store, s) for s in store.list_sources()]
    failed = [
        r.source.id for r in results
        if not r.stored_ok or r.external_status == "drift"
    ]
    audit.log_event(
        store.kb_dir, event="source.verify", actor=actor,
        object_ids=failed, data={"checked": len(results), "failed": len(failed)},
    )
    return results
