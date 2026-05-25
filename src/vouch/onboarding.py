"""First-run KB content used by `vouch init`."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Claim, ClaimStatus, ClaimType, Source
from .storage import ArtifactNotFoundError, KBStore, sha256_hex

STARTER_SOURCE_TEXT = """# Vouch starter source

This starter source is created by `vouch init` so new users can see how a
reviewed claim cites durable evidence.

Keep facts small, cite their sources, and approve only the knowledge you want
future agents to retrieve.
"""

STARTER_CLAIM_ID = "vouch-starter-reviewed-knowledge"
STARTER_CLAIM_TEXT = (
    "Vouch stores reviewed, cited knowledge in the repository so future agent "
    "sessions can retrieve agreed project context."
)


@dataclass(frozen=True)
class StarterSeedResult:
    source_id: str
    claim_id: str
    created_source: bool
    created_claim: bool

    @property
    def created_anything(self) -> bool:
        return self.created_source or self.created_claim


def seed_starter_kb(
    store: KBStore, *, approved_by: str = "vouch-init"
) -> StarterSeedResult:
    source, created_source = _starter_source(store)
    created_claim = _starter_claim(store, source_id=source.id, approved_by=approved_by)
    return StarterSeedResult(
        source_id=source.id,
        claim_id=STARTER_CLAIM_ID,
        created_source=created_source,
        created_claim=created_claim,
    )


def _starter_source(store: KBStore) -> tuple[Source, bool]:
    body = STARTER_SOURCE_TEXT.encode("utf-8")
    source_id = sha256_hex(body)
    created = not (store.kb_dir / "sources" / source_id / "meta.yaml").exists()
    source = store.put_source(
        body,
        title="Vouch starter source",
        locator="vouch:init",
        source_type="message",
        media_type="text/markdown",
        tags=["vouch", "onboarding"],
    )
    return source, created


def _starter_claim(store: KBStore, *, source_id: str, approved_by: str) -> bool:
    try:
        store.get_claim(STARTER_CLAIM_ID)
        return False
    except ArtifactNotFoundError:
        claim = Claim(
            id=STARTER_CLAIM_ID,
            text=STARTER_CLAIM_TEXT,
            type=ClaimType.WORKFLOW,
            status=ClaimStatus.ACTIONABLE,
            confidence=0.95,
            evidence=[source_id],
            tags=["vouch", "onboarding"],
            approved_by=approved_by,
        )
        store.put_claim(claim)
        return True
