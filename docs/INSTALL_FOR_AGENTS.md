# install vouch for agents

this is a deterministic, top-to-bottom checklist for an ai coding agent
dropped into a fresh repository. read it in order and run each numbered step;
every step is a concrete command with a concrete assertion on its output, so
you can branch on success or failure instead of pattern-matching prose.
`docs/getting-started.md` is the human-facing tour of the same loop — this
file is the machine-followable counterpart.

two rules before you start:

- **you propose; a human approves.** `vouch propose-*` writes a pending
  proposal under `.vouch/proposed/` (gitignored, local only). approval is a
  human step (`vouch approve`). never approve, never self-approve, and never
  hand-write a durable file under `.vouch/claims/`, `.vouch/pages/`, or any
  other approved location.
- **everything is local.** only the current working directory's `.vouch/` and
  your host's own config tree are touched. no network call is required.

## 1. confirm vouch is installed

```bash
vouch --version
```

if this fails, install from a clone (see `AGENTS.md` → install) or run
`pipx install vouch-kb`, then re-run. do not continue until it prints a
version.

## 2. detect your host and wire the mcp server

list the host adapters this build ships, then match your own host name against
the printed set:

```bash
vouch install-mcp --list
```

the output is a bullet list of host names, for example `claude-code`,
`cursor`, `codex`, `windsurf`, `zed`, `cline`, `continue`, `claude-desktop`,
`openclaw`. pick the entry that matches the host you are running inside. if
none matches, stop and tell the user which host you are and that no adapter is
available — do not guess a host.

wire the server for your host (`<host>` is the name you matched):

```bash
vouch install-mcp <host>
```

this is idempotent: you may run it unconditionally. it reports every file as
`written`, `appended`, `merged`, or `skipped` and ends with a one-line
summary, so a rerun is a flat no-op. options:

- `--tier T1|T2|T3|T4` selects how much to install; tiers stack (`T1` = mcp
  wire only, up through `T4` = host hooks/settings). the default is `T4`.
- `--path <dir>` (alias `--target <dir>`) installs into a project tree other
  than the current directory.

## 3. verify the kb.* surface

```bash
vouch capabilities
```

this prints the json capabilities descriptor. assert on three fields:

- `.name` equals `"vouch"`.
- `.review_gated` equals `true`.
- `.methods` contains the verbs you will use — at minimum `kb.propose_claim`,
  `kb.approve`, and `kb.list_pending`.

with `jq`:

```bash
vouch capabilities | jq '{name, review_gated, has_propose: (.methods | index("kb.propose_claim") != null)}'
# => { "name": "vouch", "review_gated": true, "has_propose": true }
```

if `.name` is not `"vouch"` or `.review_gated` is not `true`, you are not
talking to a vouch server — stop.

## 4. point at (or create) a kb

if the repository has no `.vouch/` directory, create one:

```bash
vouch init
```

if `.vouch/` already exists, confirm it instead of re-initialising:

```bash
vouch status
```

`vouch status` prints the durable artifact counts (claims, pages, sources,
entities, relations) and the pending-proposal count. record the claim count —
you will assert that it goes up by one in step 5.

## 5. smoke-test one propose → approve round trip

register a source, propose a claim that cites it, and confirm the proposal is
pending. these three steps are yours to run:

```bash
vouch source add <file>            # prints the source id
vouch propose-claim --text "<a fact drawn from that file>" --source <source-id>
vouch pending                      # lists your proposal as [claim] by <you>
```

`vouch pending` must show the proposal you just created, attributed to you and
still awaiting review.

the next step is **a human's**, not yours. a reviewer approves the pending
proposal:

```bash
vouch approve <proposal-id> --reason "<why it is correct>"
```

do not run `vouch approve` yourself: approval requires a different actor than
the proposer, so approving your own proposal is rejected by design. once a
human has approved it, the durable claim exists and the count moves:

```bash
vouch status
# durable: <n+1> claims  •  … •  pending: 0 proposals
```

the claim total is now one higher than the value you recorded in step 4 and
the pending count is back to zero. the round trip is complete: you proposed, a
human approved, and the reviewed claim is now part of the kb.
