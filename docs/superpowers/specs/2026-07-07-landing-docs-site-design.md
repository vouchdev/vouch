# landing page redesign + published docs site — design

date: 2026-07-07
status: approved-direction, pending user review of this spec
decided with user: Mintlify for docs; landing source lives in this repo; ship
site + docs before the codebase upgrade phases.

## context

A competitor analysis of pmb (pmbai.dev, `oleksiijko/pmb`) — the closest
comparable product (local-first agent memory, SQLite, MCP-native) — showed
its public surface converts because of three things vouch lacks:

1. a landing page with visuals and led-by-numbers proof points,
2. a published docs site (docs.pmbai.dev, Mintlify) with a clear
   Get started / Guides / Concepts / Reference taxonomy,
3. registry distribution (GitHub MCP Registry listing).

Meanwhile vouch's current situation: vouchai.dev is live (Netlify) but
text-only, and its source is **not in this repo and not on this machine** —
it cannot be PR'd against, which is exactly the drift failure pmb's own
landing page suffers (its site numbers contradict its README). The repo's
`web/` directory is untracked local scratch with narrower Gittensor-only
positioning. Vouch's docs are extensive but flat, partially stale, and only
readable as GitHub markdown.

Vouch's structural advantages to foreground: the review gate, typed
human-approved relations + provenance (vs pmb's untyped co-occurrence
graph), citations to content-hashed sources, git-native plaintext storage,
and a real measured A/B (17% faster, 32% fewer turns, 18% cheaper —
currently buried in `docs/tutorials/remember-across-sessions.md`).

## scope

Three tracks, this spec only:

- **track 1** — landing page redesign, source committed to this repo,
  deployed to the existing vouchai.dev Netlify site.
- **track 2** — Mintlify docs site at docs.vouchai.dev, source in this repo.
- **track 3** — distribution: GitHub MCP Registry listing + README hero
  rendering on PyPI.

**not in scope here** (separate spec, after the site ships): the codebase
upgrade phases from the same analysis — fusion-by-default retrieval, ranked
recall digest, UserPromptSubmit per-prompt injection, claim-usage telemetry,
propose-time dup flagging, SLO-as-code. Tracked in
`proposed-features.md` / follow-up spec.

## track 1 — landing page

### where it lives

- new top-level `site/` directory, committed. plain hand-written
  HTML/CSS/JS — no framework, no build step, no CDN dependencies
  (self-contained assets only; pmb's live dashboard breaks offline because
  of a unpkg script — we don't repeat that).
- `netlify.toml` at repo root: publish `site/`, no build command.
- deploy: the existing vouchai.dev Netlify site gets linked to the GitHub
  repo (user action, see prerequisites) so every merge to `main` deploys.
  until linked, manual deploys via `netlify deploy --prod --dir site/` work.
- the untracked `web/` scratch dir stays untracked and is superseded by
  `site/`; delete locally once the new site ships.

### page structure (single page + two support pages)

`index.html`:

1. **hero** — keep the working headline ("The memory your agent has to
   earn") unless user objects; subline states the invariant: local-first
   knowledge base for coding agents, every write passes review. install
   one-liner (`pipx install vouch-kb`) visible in the hero. two CTAs:
   Get started (docs quickstart) + GitHub.
2. **the loop, visualized** — a self-contained animated SVG/CSS diagram of
   propose → review → approve → compile → recall. this replaces the current
   wall-of-text "what it does" as the first visual.
3. **proof strip with reproduction links** — 17% faster / 32% fewer turns /
   18% cheaper, each linking to the tutorial that produced it. rule: no
   number on the page without a repro command or source link.
4. **the knowledge graph** — an interactive canvas render of a KB graph
   (typed, cited, human-approved edges; colored by artifact type/status).
   user decision 2026-07-07: **mock data is fine** — a hand-crafted
   demonstration KB checked into `site/assets/` as static JSON + a small
   self-contained canvas renderer (~pmb's viz.py export pattern, no CDN).
   can be swapped for a real `vouch graph --format json` export later.
   caption contrasts: co-occurrence graphs guess; these edges were
   approved by a human.
5. **pages are the product** — a rendered compiled wiki page with visible
   `[claim: …]` citations; one paragraph on `vouch compile` machine-verifying
   every citation before a draft even reaches review.
6. **straight from the audit log** — keep the existing section (it works),
   restyled as terminal-style event cards.
7. **works with your tools** — Claude Code / Cursor / Codex / Windsurf /
   Zed / OpenClaw row, linking to per-host install guides on the docs site.
8. **FAQ** — 5-6 questions (why a review gate, what happens at scale, team
   use, is my data local, how is this different from memory tools —
   answered without naming competitors).
9. **footer** — install, docs, GitHub, PyPI, X (@vouch_dev), MIT, SPEC.

support pages: `how-it-works.html` (deeper walkthrough incl. the 110s
video), `gittensor.html` (existing case study, kept as a page). the old
"Reference" nav item now points at docs.vouchai.dev.

### design language

**quality bar (user, 2026-07-07): visibly better than pmbai.dev.** the page
must lead with visuals, not copy — judged side-by-side against pmbai.dev
screenshots before shipping.

dark-first, text-forward precision retained (it reads as engineering
credibility), but with the three visuals above so the page is no longer
copy-only. system font stack or a single self-hosted font. respects
`prefers-color-scheme`; both themes styled. no analytics scripts —
consistent with the local-first claim, and say so in the footer.

### voice

sentence-case headings, no marketing "we/let's", every claim mechanical and
verifiable. no gbrain references anywhere. generic placeholder names in any
sample data (`alice-example`, `acme-example`).

## track 2 — docs site (Mintlify)

### where it lives

- new top-level `mintlify/` directory (mirrors pmb's layout): `docs.json`,
  MDX pages, `logo/`, `images/`.
- content is **ported and upgraded** from `docs/` — not duplicated
  wholesale. porting fixes the known rot in one pass: stale
  `getting-started` install text (pip -e → `pipx install vouch-kb`),
  `docs/README.md` index omissions, `llms.txt` regeneration pointing at the
  site.
- drift policy (explicit, because pmb got this wrong): `mintlify/` becomes
  the canonical **user-facing** docs; `docs/` keeps only what is
  repo-internal (SPEC.md stays root + normative, `docs/superpowers/`,
  images/media, `demo.tape`). each moved `docs/*.md` is replaced by a
  two-line pointer stub to its docs-site page so old links don't rot.

### navigation taxonomy

- **Get started**: introduction (mini landing: what/why/60-second mental
  model), quickstart (init → install-mcp → first session → review →
  compile), install (per-host matrix).
- **Tutorials**: the 5 existing Diátaxis tutorials, ported as-is (their
  "every command was run before it went in the docs" contract is stated on
  the section index).
- **Concepts**: object model, the review gate, provenance & audit,
  retrieval, compile (llm-wiki framing — pages are the product),
  company brain / team use.
- **Guides**: per-host integration (claude-code, cursor, codex, windsurf,
  zed, openclaw, generic MCP), review UI, sessions & capture, embeddings,
  bundles/export-import, migrations, multi-agent, gittensor.
- **Reference**: CLI commands, MCP tool surface (58 tools), JSONL protocol,
  capabilities & method list, metrics, config keys, FAQ; SPEC.md linked as
  the normative source ("when docs and spec disagree, the spec wins").
- **Contributing**: contributing, roadmap (refreshed — current ROADMAP.md
  contradicts the shipped product and gets rewritten as part of this track).

### theme

brand color pulled from `docs/banner.svg` palette, dark default, GitHub
navbar link + "Get started" button — the standard Mintlify frame pmb uses,
with vouch branding.

## track 3 — distribution

- `server.json` (MCP registry schema) at repo root registering
  `io.github.vouchdev/vouch-kb` (pypi package, stdio transport), plus the
  `<!-- mcp-name: … -->` ownership comment as README line 1, plus a
  "GitHub MCP Registry — listed" badge once accepted.
- README hero assets switched to raw.githubusercontent.com URLs so PyPI and
  the registry render them.
- (deferred, optional) npm thin-launcher shim like pmb's `pmb-ai` package.

## verification

- **site**: `netlify dev`/`python -m http.server` local preview; a
  Playwright pass screenshotting every section in both themes at
  desktop/mobile widths; a link checker over all hrefs; page loads with
  network disabled except first-party assets (proves no-CDN claim).
- **docs**: `mint dev` local preview; `mint broken-links`; every command in
  quickstart re-run against the current build before publish (tutorial
  contract extended to the whole Get started section).
- **numbers discipline**: a checklist in the PR description mapping every
  number on the site to its source; version/number claims covered by the
  existing manifest-version tests where applicable.
- CI: a lightweight `site-check` job (link check + HTML validation) added
  to ci.yml; Mintlify validates on its own deploy.

## prerequisites needing user action

1. **Netlify**: link the existing vouchai.dev site to the GitHub repo
   (Netlify dashboard → site → build settings), or hand me
   `NETLIFY_AUTH_TOKEN` + site ID as repo secrets for a deploy workflow.
   also: locate/retire the old site source so nothing else deploys over us.
2. **Mintlify**: create the (free OSS) Mintlify project, connect the GitHub
   repo, set docs directory to `mintlify/`, and add a
   `docs.vouchai.dev` CNAME DNS record.
3. **MCP Registry**: the publish step authenticates via GitHub as the repo
   owner (plind-junior / vouchdev org) — a one-time `mcp-publisher` run.

## risks

- **two docs trees drifting** — mitigated by the pointer-stub policy and by
  moving, not copying, user-facing pages.
- **Netlify cutover** — until the old deploy source is identified/retired,
  a stray deploy could overwrite the new site; mitigated by doing the repo
  link first and verifying deploy provenance in the Netlify dashboard.
- **Mintlify SaaS dependency** — accepted trade-off (user decision); the
  MDX source stays in-repo so migration to Starlight/MkDocs later is
  mechanical.
- **graph asset staleness** — the checked-in graph JSON is a snapshot;
  regenerate via a documented `make site-assets` target rather than by hand.
