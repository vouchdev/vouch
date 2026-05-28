"""First-run KB content used by `vouch init`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Claim, ClaimStatus, ClaimType, Entity, EntityType, Source
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


# --- template registry ----------------------------------------------------

DEFAULT_TEMPLATE = "starter"


@dataclass(frozen=True)
class SeedResult:
    """Outcome of seeding a named template — the ids actually created."""

    template: str
    created: list[str]

    @property
    def created_anything(self) -> bool:
        return bool(self.created)


GITTENSOR_ENTITY_ID = "gittensor-sn74"
GITTENSOR_SOURCE_TEXT = """# Gittensor (SN74)

Gittensor is a Bittensor subnet (SN74) that rewards open-source contributions.
Miners register a fine-grained GitHub personal access token and contribute to
whitelisted repositories. When their pull requests are merged, validators
verify account ownership via the PAT and score the merged contributions by
code quality, repository allocation, and programming-language factors. GitHub
account verification and the merged-PR requirement make the subnet
sybil-resistant.
"""

_GITTENSOR_CLAIMS: list[tuple[str, str]] = [
    (
        "gittensor-rewards-merged-prs",
        "Gittensor (SN74) rewards miners with TAO for pull requests merged into "
        "whitelisted open-source repositories.",
    ),
    (
        "gittensor-validators-verify-pat",
        "Gittensor validators verify GitHub account ownership via a fine-grained "
        "personal access token before scoring contributions.",
    ),
    (
        "gittensor-scoring-factors",
        "Gittensor scores merged contributions by code quality, repository "
        "allocation, and programming-language factors.",
    ),
    (
        "gittensor-sybil-resistance",
        "Gittensor is sybil-resistant: GitHub account verification and a merged-PR "
        "requirement prevent gaming.",
    ),
]


def seed_gittensor_kb(store: KBStore, *, approved_by: str = "vouch-init") -> SeedResult:
    """Seed a cited, approved starter pack about Gittensor (SN74) scoring.

    Idempotent: stable ids mean a second call creates nothing.
    """
    created: list[str] = []

    body = GITTENSOR_SOURCE_TEXT.encode("utf-8")
    source_id = sha256_hex(body)
    if not (store.kb_dir / "sources" / source_id / "meta.yaml").exists():
        created.append(source_id)
    source = store.put_source(
        body,
        title="Gittensor SN74",
        locator="vouch:template/gittensor",
        source_type="message",
        media_type="text/markdown",
        tags=["gittensor", "sn74", "onboarding"],
    )

    try:
        store.get_entity(GITTENSOR_ENTITY_ID)
    except ArtifactNotFoundError:
        store.put_entity(Entity(
            id=GITTENSOR_ENTITY_ID,
            name="Gittensor SN74",
            type=EntityType.PROJECT,
            description=(
                "Bittensor subnet SN74 that rewards merged open-source "
                "contributions with TAO."
            ),
        ))
        created.append(GITTENSOR_ENTITY_ID)

    for claim_id, text in _GITTENSOR_CLAIMS:
        try:
            store.get_claim(claim_id)
        except ArtifactNotFoundError:
            store.put_claim(Claim(
                id=claim_id,
                text=text,
                type=ClaimType.FACT,
                status=ClaimStatus.STABLE,
                confidence=0.9,
                evidence=[source.id],
                entities=[GITTENSOR_ENTITY_ID],
                tags=["gittensor", "sn74"],
                approved_by=approved_by,
            ))
            created.append(claim_id)

    return SeedResult(template="gittensor", created=created)


# Non-default templates dispatched by `vouch init --template`. The default
# `starter` seed keeps its own bespoke output, so it isn't registered here.
TEMPLATES: dict[str, Callable[..., SeedResult]] = {
    "gittensor": seed_gittensor_kb,
}


def available_templates() -> list[str]:
    return sorted({DEFAULT_TEMPLATE, *TEMPLATES})
