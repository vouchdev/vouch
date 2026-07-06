"""First-run KB content used by `vouch init`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import yaml

from .models import (
    Claim,
    ClaimStatus,
    ClaimType,
    Entity,
    EntityType,
    Page,
    PageStatus,
    PageType,
    Source,
)
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

# The "Edit in Obsidian" walkthrough required by #181's acceptance: a one-
# page primer that explains the bidirectional sync from the KB's side, so
# new users discover the workflow the moment they `vouch init`.
STARTER_PAGE_ID = "edit-in-obsidian"
STARTER_PAGE_BODY = """# Edit in Obsidian

Vouch's pages are plain markdown with YAML frontmatter -- your knowledge
base is already an Obsidian-compatible vault. To edit pages in your own
Obsidian vault:

1. Run `vouch sync --vault ~/Obsidian/YourVault` once to mirror approved
   pages and claims under `<vault>/vouch/`.
2. Open `<vault>/vouch/pages/<id>.md` in Obsidian and edit it.
3. Re-run `vouch sync --vault ~/Obsidian/YourVault` to file your edits as
   page-edit proposals in `.vouch/proposed/`.
4. Review and approve with `vouch approve <id>`. The next sync mirrors the
   approved version back into the vault.

Claims appear as stub markdown files under `<vault>/vouch/claims/`; pages
that cite them are linked via Obsidian `[[wikilink]]` syntax so the graph
view connects them. Use `--watch` to keep a polling loop alive while you
edit.
"""


@dataclass(frozen=True)
class StarterSeedResult:
    source_id: str
    claim_id: str
    page_id: str
    created_source: bool
    created_claim: bool
    created_page: bool

    @property
    def created_anything(self) -> bool:
        return self.created_source or self.created_claim or self.created_page


def seed_starter_kb(store: KBStore, *, approved_by: str = "vouch-init") -> StarterSeedResult:
    source, created_source = _starter_source(store)
    created_claim = _starter_claim(store, source_id=source.id, approved_by=approved_by)
    created_page = _starter_page(store, source_id=source.id)
    return StarterSeedResult(
        source_id=source.id,
        claim_id=STARTER_CLAIM_ID,
        page_id=STARTER_PAGE_ID,
        created_source=created_source,
        created_claim=created_claim,
        created_page=created_page,
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


def _starter_page(store: KBStore, *, source_id: str) -> bool:
    """Approved walkthrough page for the Obsidian sync flow.

    Seeded as ``status: active`` so `vouch sync --vault` mirrors it
    immediately on first run, and new users see the workflow inside
    Obsidian without having to read external docs first.
    """
    try:
        store.get_page(STARTER_PAGE_ID)
        return False
    except ArtifactNotFoundError:
        page = Page(
            id=STARTER_PAGE_ID,
            title="Edit in Obsidian",
            body=STARTER_PAGE_BODY,
            type=PageType.WORKFLOW,
            status=PageStatus.ACTIVE,
            claims=[STARTER_CLAIM_ID],
            sources=[source_id],
            tags=["vouch", "onboarding", "obsidian"],
        )
        store.put_page(page)
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
    (
        "gittensor-repo-allowlist",
        "Only repositories on the Gittensor allow-list count toward emissions; "
        "adding or de-listing a repo is a maintainer policy decision.",
    ),
    (
        "gittensor-issue-multiplier",
        "Solved issues earn a scoring multiplier on top of merged-PR rewards, with "
        "a higher multiplier when the solver is a maintainer of the repository.",
    ),
    (
        "gittensor-emission-split",
        "Gittensor emissions are split between OSS-contribution rewards and an "
        "issue-treasury share; the split is a validator policy decision.",
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
        store.put_entity(
            Entity(
                id=GITTENSOR_ENTITY_ID,
                name="Gittensor SN74",
                type=EntityType.PROJECT,
                description=(
                    "Bittensor subnet SN74 that rewards merged open-source contributions with TAO."
                ),
            )
        )
        created.append(GITTENSOR_ENTITY_ID)

    for claim_id, text in _GITTENSOR_CLAIMS:
        try:
            store.get_claim(claim_id)
        except ArtifactNotFoundError:
            store.put_claim(
                Claim(
                    id=claim_id,
                    text=text,
                    type=ClaimType.FACT,
                    status=ClaimStatus.STABLE,
                    confidence=0.9,
                    evidence=[source.id],
                    entities=[GITTENSOR_ENTITY_ID],
                    tags=["gittensor", "sn74"],
                    approved_by=approved_by,
                )
            )
            created.append(claim_id)

    return SeedResult(template="gittensor", created=created)


# --- company-brain template -------------------------------------------------

BRAIN_TEMPLATE = "company-brain"
BRAIN_PAGE_ID = "company-brain-guide"

# Typed record kinds for running a team's shared memory through the review
# gate: people and orgs as entities paired with typed pages, work as
# project records, commitments as followups with a due date, decisions and
# voice pages forced to cite their evidence. Declared as plain
# `page_kinds` config so `vouch schema list` shows them and both the
# propose and approve gates validate them — no new machinery.
BRAIN_PAGE_KINDS: dict[str, dict[str, object]] = {
    "contact": {
        "description": "a person the team works with",
        "required_fields": ["role"],
        "frontmatter_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "org": {"type": "string"},
                "email": {"type": "string"},
            },
        },
    },
    "org": {
        "description": "a company or organisation the team deals with",
        "frontmatter_schema": {
            "type": "object",
            "properties": {"website": {"type": "string"}},
        },
    },
    "project-record": {
        "description": "one project's living record",
        "required_fields": ["record_status"],
        "frontmatter_schema": {
            "type": "object",
            "properties": {
                "record_status": {"type": "string"},
                "owner": {"type": "string"},
            },
        },
    },
    "meeting-notes": {
        "description": "notes from one meeting",
        "frontmatter_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "attendees": {"type": "array"},
            },
        },
    },
    "followup": {
        "description": "a dated commitment; surfaced by `vouch digest` until closed",
        "required_fields": ["due_at", "followup_status"],
        "frontmatter_schema": {
            "type": "object",
            "properties": {
                "due_at": {"type": "string"},
                "followup_status": {"type": "string"},
                "owner": {"type": "string"},
            },
        },
    },
    "decision-record": {
        "description": "why the team decided something; must cite evidence",
        "required_citations": True,
        "protected": True,
    },
    "voice": {
        "description": "how the team sounds in one channel; cites the examples it distills",
        "required_citations": True,
        "protected": True,
    },
}

BRAIN_SOURCE_TEXT = """# Company-brain starter source

Created by `vouch init --template company-brain` so the seeded guide page has
a citable source. A team's shared memory works when every record — contact,
project, followup, decision — is proposed with evidence and approved by a
human before agents rely on it.
"""

BRAIN_PAGE_BODY = """# Company brain: typed records through the review gate

This KB is set up as a team memory. Records are ordinary vouch pages with a
declared kind (see `vouch schema list`), so every write is proposed, reviewed,
and audited like any other knowledge.

Conventions:

1. A person = `kb_propose_entity` (type `person`) plus a `contact` page
   (frontmatter: `role`, `org`, `email`). An organisation = entity (type
   `company`) plus an `org` page.
2. A project = entity (type `project`) plus a `project-record` page whose
   `record_status` frontmatter tracks where it stands.
3. A commitment = a `followup` page with `due_at` and `followup_status`
   frontmatter. `vouch digest` lists open followups that are due; close one
   by re-proposing the page with `followup_status: done`.
4. A decision = a `decision-record` page. It must cite the claims or sources
   it rests on — the gate rejects an uncited one.
5. Meeting notes go in `meeting-notes` pages; durable facts distilled from
   them become claims via `kb_propose_claim`, citing a registered source.

Daily loop: agents file proposals all day; a human reviews with
`vouch review` or `vouch approve <ids>`; `vouch digest` (run it from cron)
shows what is pending, what was decided, which claims went stale, and which
followups are due.
"""


def seed_company_brain_kb(store: KBStore, *, approved_by: str = "vouch-init") -> SeedResult:
    """Declare the typed record kinds and seed a cited guide page.

    Idempotent: kinds the operator already declared are left untouched, and
    stable artifact ids mean a second call creates nothing.
    """
    created: list[str] = []

    created += [f"page_kinds.{k}" for k in _merge_page_kinds(store, BRAIN_PAGE_KINDS)]

    body = BRAIN_SOURCE_TEXT.encode("utf-8")
    source_id = sha256_hex(body)
    if not (store.kb_dir / "sources" / source_id / "meta.yaml").exists():
        created.append(source_id)
    source = store.put_source(
        body,
        title="Company-brain starter source",
        locator="vouch:template/company-brain",
        source_type="message",
        media_type="text/markdown",
        tags=["company-brain", "onboarding"],
    )

    try:
        store.get_page(BRAIN_PAGE_ID)
    except ArtifactNotFoundError:
        store.put_page(
            Page(
                id=BRAIN_PAGE_ID,
                title="Company brain: typed records through the review gate",
                body=BRAIN_PAGE_BODY,
                type=PageType.WORKFLOW,
                status=PageStatus.ACTIVE,
                sources=[source.id],
                tags=["company-brain", "onboarding"],
            )
        )
        created.append(BRAIN_PAGE_ID)

    return SeedResult(template=BRAIN_TEMPLATE, created=created)


def _merge_page_kinds(store: KBStore, kinds: dict[str, dict[str, object]]) -> list[str]:
    """Add missing `page_kinds` entries to config.yaml; return the names added.

    When the file has no `page_kinds` key at all the block is appended
    textually so operator comments and formatting survive. When the key
    exists the file is parsed, missing kinds are filled in, and the mapping
    is re-dumped (a yaml round-trip drops comments — the narrow price of a
    real merge).
    """
    path = store.kb_dir / "config.yaml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        loaded = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"config.yaml is not valid yaml; fix it first: {e}") from e
    if not isinstance(loaded, dict):
        raise ValueError("config.yaml must be a yaml mapping")

    existing = loaded.get("page_kinds")
    if not isinstance(existing, dict):
        block = yaml.safe_dump(
            {"page_kinds": kinds}, sort_keys=False, default_flow_style=False
        )
        prefix = text.rstrip("\n")
        joined = f"{prefix}\n\n{block}" if prefix else block
        path.write_text(joined, encoding="utf-8")
        return sorted(kinds)

    added = sorted(k for k in kinds if k not in existing)
    if not added:
        return []
    for name in added:
        existing[name] = kinds[name]
    path.write_text(
        yaml.safe_dump(loaded, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return added


# Non-default templates dispatched by `vouch init --template`. The default
# `starter` seed keeps its own bespoke output, so it isn't registered here.
TEMPLATES: dict[str, Callable[..., SeedResult]] = {
    "gittensor": seed_gittensor_kb,
    BRAIN_TEMPLATE: seed_company_brain_kb,
}


def available_templates() -> list[str]:
    return sorted({DEFAULT_TEMPLATE, *TEMPLATES})
