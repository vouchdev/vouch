# install for agents

machine-readable, top-to-bottom checklist for an assistant dropped into a
fresh project. every step is a concrete `vouch` command plus an assertion on
its output. if a step fails, stop and fix it before continuing.

this doc is for agents. humans should start with
[getting-started.md](getting-started.md).

## prerequisites

1. confirm `vouch` is on `PATH`:

   ```bash
   vouch --version
   ```

   **assert:** prints a version string and exits 0.

2. `cd` to the project root you are wiring (the tree that will hold `.vouch/`).

## 1. detect the host

```bash
vouch install-mcp --list
```

**assert:**

- exits 0.
- prints a bullet list of adapter names (for example `claude-code`, `cursor`,
  `codex`, `windsurf`, `zed`).
- your host name appears in that list. if it does not, stop — pick the closest
  supported adapter or wire `vouch serve` manually per
  [transports.md](transports.md).

## 2. wire the server

replace `<host>` with the name from step 1.

```bash
vouch install-mcp <host>
```

optional flags (all real — verified against `cli.py`):

- `--tier T1|T2|T3|T4` — how much to install; tiers stack (default `T4`).
- `--path <dir>` or `--target <dir>` — project root to write into (default `.`).
- `--no-init` — do not bootstrap a KB when `.vouch/` is missing.

when no `.vouch/` is discoverable at or above the target, this command
bootstraps one first (same path as `vouch init`; staging-dir hosts like
`claude-desktop` are exempt) — expect a `No .vouch/ found — initialised KB
at …` line in that case. re-runs are flat-noop: expect lines containing
`written`, `appended`, `merged`, or `skipped`. you may run this command
unconditionally on every session start.

**assert:** exits 0; no `error:` lines.

## 3. verify the kb.* surface

```bash
vouch capabilities
```

**assert** on the JSON object:

- `.name` is `"vouch"`.
- `.review_gated` is `true`.
- `.methods` includes at least:
  - `kb.propose_claim`
  - `kb.list_pending`
  - `kb.approve`

(`kb.approve` is exposed for trusted hosts; agents must still not call it —
see step 6.)

## 4. create or locate a kb

step 2 already bootstraps the KB when it is missing, so normally there is
nothing to create here. if you wired with `--no-init` (or skipped step 2):

```bash
vouch init
```

**assert:** prints a path under the project and creates `.vouch/config.yaml`.

if `.vouch/` already exists:

```bash
vouch status
```

**assert:** prints `KB at …` with artifact counts and a `pending:` line.

## 5. smoke test — propose (agent)

create a throwaway citation file and register it:

```bash
printf 'vouch agent install smoke test\n' > /tmp/vouch-agent-smoke.txt
vouch source add /tmp/vouch-agent-smoke.txt --title "agent smoke test"
```

**assert:** prints a 64-character hex source id (sha256 content address).

propose a claim citing that source (replace `<source-id>`):

```bash
vouch propose-claim \
  --text "vouch agent install smoke test passed." \
  --source <source-id> \
  --type observation \
  --confidence 0.9
```

**assert:** prints a proposal id (for example `20260707-…`).

confirm it is pending:

```bash
vouch pending
```

**assert:** lists the proposal id from the previous step with `[claim]`.

## 6. smoke test — approve (human only)

**stop — this step is for the human reviewer, not the agent.**

the agent must not run `vouch approve`, must not self-approve, and must not
hand-write files under `.vouch/claims/`, `.vouch/pages/`, or other `decided/`
paths. proposals live in `.vouch/proposed/` (gitignored) until a human decides.

the human runs:

```bash
vouch approve <proposal-id> --reason "agent install smoke test"
vouch status
```

**assert:** `vouch status` shows the durable claim count incremented by one
compared to the count before step 5.

## 7. what agents must never do

- call `vouch approve` or `kb.approve` — the review gate is human-held.
- write yaml or markdown directly into `.vouch/claims/`, `.vouch/pages/`, or
  other approved artifact directories.
- skip citation: every `propose-claim` needs at least one `--source` id.

## where next

- human-oriented walkthrough: [getting-started.md](getting-started.md)
- protocol and method shapes: [../SPEC.md](../SPEC.md)
- host-specific manifests: [../adapters/](../adapters/)
