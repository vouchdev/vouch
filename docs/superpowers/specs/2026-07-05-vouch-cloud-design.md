# vouchhub — hosted, multi-tenant vouch (phase 2)

- status: draft, awaiting review
- date: 2026-07-05
- scope: phase-2 product design (cloud deployment + /vouch-report skill);
  implementation plans per milestone come after review
- decision method: three independent architecture designs (thin-sync /
  cloud-primary-platform / git-native-hubs) scored by a three-lens judge
  panel (invariants, velocity, security); this spec is the synthesis of
  the winner plus grafts the judges named from the losing designs

## goal

vouch's knowledge today lives on one laptop. phase 2 makes the same
review-gated KB reachable from anywhere — "agent OS": a user's agents
(claude code, cursor, codex, ci bots) on any machine propose into ONE
personal KB over an authenticated remote MCP endpoint; the human reviews
from a browser anywhere; approved knowledge recalls into every future
session. login with github or google, per-user isolation, plans and
billing, and full feature parity with the local review ui.

## governance note (read first)

CLAUDE.md currently lists "a SaaS mode / hosted vouch" as explicitly out
of scope ("local-first by design"). the owner's phase-2 direction repeals
that. before any phase-2 code lands, CLAUDE.md and ROADMAP.md must be
amended — otherwise every contributor and agent working the repo will
correctly push back on the PRs. the local-first *invariant* is not
repealed: local vouch keeps working with zero cloud dependency, and the
cloud must never become a second source of truth that can drift from or
bypass the gate.

## the decision

judge matrix (0-10 per lens):

| design | invariants | velocity | security | total |
|---|---|---|---|---|
| A — thin sync, local primary | **9** | 7 | 7.5 | 23.5 |
| B — cloud-primary postgres platform | 4 | 4 | **8.5** | 16.5 |
| C — git-native hosted hubs | 7.5 | **8.5** | 6.5 | 22.5 |

B is eliminated: it inverts where truth lives (postgres, not plaintext
files), rewrites storage.py behind a conformance suite, and wins only on
tenancy-by-RLS — a property the file-per-tenant designs get by
construction. A and C are cousins (files as truth, postgres for identity
only, reuse of the existing review ui and MCP handlers); they differ on
where a KB is born and how sync works.

**synthesis: C's runtime with A's trust rules.** cloud KBs are
server-side git repositories running the existing vouch code (C — the
fast, code-reusing path: every load-bearing component already exists and
was verified working in the 2026-07-05 feature sweep). the risky piece A
front-loads — bidirectional git sync with audit-hash-chain rebase — is
deferred to a post-MVP milestone in A's safest form (fast-forward-only
verified push). grafts adopted from the judges: device-code CLI login
(A), one-git-commit-per-decision with real human author attribution (C,
endorsed by the invariants judge), day-one source-blob size cap + LFS
decision (C), bundle upload via the existing gated import path as the
zero-new-code inbound channel (C), token-provenance tiering — sync-class
credentials only from an interactive human flow (A, endorsed by the
security judge), reads-never-blocked billing (C).

## name

**VouchHub** (hub.vouch.dev now; vouchhub.dev NXDOMAIN today, likely
registrable). the git→github analogy is not marketing — it is literally
the architecture: local-first plaintext repos, hosted hubs. keeps 100%
of the vouch brand equity a 1-2 dev team can't afford to split. naming
sweep results: "vouchsafe", "attest", "countersign" eliminated (existing
funded products); runner-up if maximum legal cleanliness is wanted:
**vouchpoint** (vouchpoint.dev). tagline: *"vouch for what your agents
know — from anywhere."* the local tool stays `vouch`.

## north-star fit

the review gate survives verbatim and in exactly one place:
`proposals.approve()` on the tenant's store, called only from the
human-gated review-ui routes. hubd introduces **no new decision code
path**. agent tokens are *structurally* incapable of approving — the MCP
tool registry served to a read+propose token does not contain
kb_approve/kb_reject at all. "cloud may never auto-approve" holds by
construction, not policy.

## architecture

one python 3.12 monolith ("hubd") on a single disk-heavy VM (hetzner
CX42-class; fly.io alternative), caddy for TLS, managed postgres (neon).
hubd = one fastapi app, four sub-surfaces:

1. **control plane** — /auth/*, /account, /kbs crud, /tokens, /billing.
2. **review ui** — the existing `src/vouch/web/server.py` routes mounted
   under `/{user}/{kb}/…` via a TenantContext middleware.
3. **remote MCP** — FastMCP streamable-http sub-app at `/mcp/{user}/{kb}`;
   tenant resolved from the bearer token into a contextvar so the
   existing kb_* tools get the right KBStore. plus JSONL-over-HTTP at
   `POST /api/{user}/{kb}/rpc` (same envelope as jsonl_server).
4. **git smart HTTP** at `/git/{user}/{kb}.git` — clone/fetch only in
   MVP, shelling to `git http-backend` with the repo path resolved from
   postgres (urls are never turned into paths).

disk: `/srv/hub/work/{user_id}/{kb_id}/` is a non-bare git repo whose
root holds `.vouch/` — claims/pages/sources/decided/audit committed;
`proposed/` and `state.db` gitignored, exactly as local vouch.

write flows: agent propose = token hash lookup → TenantContext (KBStore +
per-KB `fcntl.flock`) → the same `proposals.propose_claim()` → websocket
broadcast. human approve = session cookie → existing /approve handler →
`proposals.approve()` → post-hook `git commit --author "Alice <email>"`
(**one decision = one commit** — the server history becomes a
per-decision diffable record with real attribution). non-committing
audit lines sweep into the next decision commit or an hourly checkpoint
commit. single uvicorn worker per node (in-process websocket hubs);
scale-out = shard tenants across nodes, never shared volumes. nightly
restic backups to B2; optional user-configured mirror-push to their own
github repo. dual_solve disabled in cloud.

## data model

postgres holds identity and money, **never knowledge**: `users`,
`oauth_identities` (github + google linkable by verified email),
`web_sessions` (revocable cookies), `agent_tokens` (sha256 hash, scopes
text[], per-KB scoping, revoked_at), `kbs` (owner, slug, disk_path),
`kb_members` (roles table ships day one, owner-only in MVP), `plans`,
`subscriptions` (stripe), `entitlements` (kind, granted_via, proof jsonb,
expires_at), `usage_counters`, `control_audit` (logins/token mints —
distinct from KB audit logs).

files/git per tenant hold all knowledge. per-tenant `state.db` is
derived and lazily rebuilt via the existing `kb.index_rebuild` — that
invariant is what makes restore and tenant migration trivial.
**day-one irreversible call:** source content blobs capped at 5 MB and
stored outside git history (or LFS) — git history bloat from PDFs can
never be fixed later without rewriting tenant history.

## auth

- **humans**: authlib. github oauth (read:user user:email) and google
  oidc + pkce (keyed on `sub`); account linking by verified email behind
  an explicit confirm screen. session = random id in HttpOnly
  SameSite=Lax cookie referencing `web_sessions`.
- **agents**: personal access tokens `vhp_<base62-256bit>`, shown once,
  stored hashed. minted in the UI or via **device-code flow**:
  `vouch hub login` prints a url + 8-char code, user confirms in
  browser, cli receives the token. scopes: `read`, `propose`, `source`;
  a `decide` scope exists but the mint UI only offers read+propose for
  agent tokens — decide requires an explicit are-you-sure flow and is
  the cloud mirror of the local trusted-agent flag. token label becomes
  the audit actor string (per-agent attribution preserved).
- claude code setup is one line:
  `claude mcp add --transport http vouchhub https://hub.vouch.dev/mcp/alice/myproj --header "Authorization: Bearer vhp_…"`.
- phase 2.5: proper MCP oauth (rfc 8414 discovery + dynamic client
  registration); PATs ship first because every MCP host supports static
  headers today.

## tenancy & isolation

isolation is the filesystem — the strongest form for this codebase: a
KBStore is constructed per request rooted at the tenant's disk_path
resolved from postgres by kb_id (slugs never become paths → no
traversal). vouch's own code cannot see another tenant because it has no
concept of one; there is no shared table to mis-scope a WHERE clause on.
per-KB flock serializes ui writes vs MCP proposals vs git operations.
no tenant code executes (dual-solve off), so process isolation is not
required in MVP; resource isolation = storage quotas, per-token rate
limits (slowapi), request body caps.

## sync & no-lock-in (three stages)

1. **MVP — clone-out:** `git clone https://hub.vouch.dev/git/alice/myproj.git`
   yields the full .vouch/ tree + complete per-decision history; local
   vouch runs against the clone offline, forever. inbound = upload a
   `vouch export` bundle in the UI → the existing gated
   `kb.import_check` / `kb.import_apply`. zero new sync code.
2. **post-MVP — verified push (fast-forward-only):** a pre-receive hook
   rejects any pushed commit adding durable artifacts without a matching
   decided/ record and correctly-chained audit entries. honest limit,
   stated up front: the hash chain proves the gate *ran*, not *who* ran
   it — an owner can forge their own history. acceptable: the gate
   defends humans from agents, not the hub from its owner; server-side
   signing is a later compliance feature.
3. **later:** `vouch hub pull/push` sugar for non-git-fluent users.

remote MCP is inherently online; the offline path is always the local
clone. the full a-style bidirectional hash-chain rebase is deliberately
NOT scheduled — fast-forward-only + pull-before-push covers the
single-human-many-agents reality.

## pricing & eligibility

plans (stripe checkout + portal + webhooks):

| plan | price | limits |
|---|---|---|
| free (gittensor / vouch-dev contributors) | $0 | 1 KB, 200 approved claims, 500 MB, 2 agent tokens |
| solo | ~$12/mo | 5 KBs, 5k claims, 5 GB, 10 tokens, cloud embeddings |
| team (post-MVP) | ~$49/mo | shared KBs + reviewer roles, 25 GB |

enforcement is **write-side only** — a limits middleware in front of
kb.propose_*, approve, kb-create, and source registration returns a
structured 402 with an upgrade url. **reads are never blocked and git
clone always works**: a lapsed subscription can always take its
knowledge and leave. eligibility verification, concretely:

- **vouch-dev contributor**: user is github-oauth'd already → server
  checks ≥1 merged PR in vouchdev repos (search api) or org membership;
  cached in `entitlements` with proof, re-verified every 30 days.
- **gittensor (SN74)**: hotkey-ownership proof — UI shows a nonce, user
  signs with their hotkey (btcli/polkadot.js), backend verifies the
  sr25519 signature and confirms the hotkey is registered in subnet 74
  via a public finney node (substrate-interface). re-checked monthly;
  lapse degrades to read-only-plus-clone, never data loss.

## /vouch-report (phase-2 item 1 — ships first, markdown only)

when an approved claim turns out wrong, the fix is a cited
counter-proposal a human rules on — never deletion. **no new kb.\*
surface is needed**; the skill composes the existing methods and the
report is an ordinary evidence-cited claim proposal with a tag
convention (`vouch-report`, `contests:<claim-id>`).

flow: (1) resolve the claim (kb_read_claim / kb_search); stop if already
superseded/contested/archived. (2) **verify before accusing** — kb_cite
+ kb_source_verify (source drift is itself evidence) + check current
reality. (3) if the claim holds, kb_confirm it instead and say why.
(4) measure blast radius with kb_impact + kb_why. (5) register
counter-evidence as content-hashed sources. (6) file ONE
kb_propose_claim stating what is actually true, citing that evidence,
tagged. (7) stop; hand the human the decision menu — approve+supersede,
approve+contradict, archive, or reject the report. (8) the agent may
complete the supersede/contradict link only after approval and only on
request. structurally gate-safe: lifecycle.contradict/supersede require
both claims durable, so linking is impossible until a human has ruled.

files: `adapters/claude-code/.claude/commands/vouch-report.md` + the
body-identical `adapters/openclaw/skills/vouch-report/SKILL.md` mirror
(+ registration in the manifest sync test's skill list). full drafted
skill body is in the phase-2 design workflow output; it follows
vouch-recall's house style. cloud extension (~1 wk, later): a derived
`report_events` table fed from the existing websocket notify layer, a
"reported" badge on contested claims, and a queue filter — a feed, not a
new write path.

## mvp milestones (1-2 devs)

| wk | milestone |
|---|---|
| 0 | /vouch-report skill (markdown-only, ships to local vouch now); CLAUDE.md + ROADMAP amendment PR |
| 1 | M0 tenant runtime: provision `vouch init` per KB into git repos, TenantContext, per-KB flock, commit-on-decision |
| 2-3 | M1 auth: github+google oauth, web_sessions, PAT mint/revoke + device-code flow |
| 4-5 | M2 review ui multi-tenantized (the Authorizer/TenantContext refactor — upstreamable; local ui gains a cleaner auth seam) |
| 6 | M3 remote MCP + JSONL-over-HTTP + git clone-out + bundle import; **invite-only private beta** |
| 7-8 | M4 stripe billing, quotas middleware, eligibility verification (github contributor + hotkey), public launch |
| post | verified FF-only push; team KBs + reviewer roles; report feed; MCP oauth; embeddings service |

## explicitly out of scope (phase 2)

- auto-approve of anything, any "trusted cloud agent" bypass — the gate
  stays, full stop.
- bidirectional merge/rebase of diverged KBs (fast-forward-only push is
  the ceiling until real demand).
- team KBs in MVP (the tables ship; the UI doesn't).
- self-serve on-prem hub distribution (the code should not preclude it —
  hubd is a monolith over files — but it is not a phase-2 deliverable).
- replacing yaml/files with a database as the source of truth (B lost;
  this stays true in the cloud).

## risks / open questions

1. **scaling ceiling by design**: one KB = one directory = one writer
   lock; fine for thousands of solo users, serializes on hot team KBs
   (~50-150ms/commit). per-tenant caching + tenant sharding is the
   answer, and it is an ops project — accepted consciously.
2. **blob policy is irreversible** — the 5 MB cap + blobs-outside-git
   decision must be made before the first real tenant exists.
3. **asymmetric sync until verified push ships** (clone-out perfect,
   inbound = bundles). must be messaged honestly; do not promise "it's
   just git" for writes at launch.
4. **ops on 1-2 devs**: one VM SPOF; untested restic restores are
   company-ending. restore drills are a launch gate, not a nice-to-have.
5. **gittensor coupling**: free-tier eligibility depends on bittensor
   chain availability and SN74's continued existence; keep the
   substrate dependency isolated in the worker so mainline auth never
   blocks on chain RPC.
6. **name**: vouchhub.dev registrable today, vouchhub.com parked —
   register both before this spec is public.
