# Getting started

In ten minutes you'll have a vouch KB at your project root, one
approved claim, and an MCP-wired agent that can read and propose to it.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '/path/to/vouch[dev]'
```

(Once vouch is published to PyPI: `pipx install vouch`.)

Confirm:

```bash
vouch --version
vouch capabilities | jq .name
# → "vouch"
```

## 2. Initialise a KB

From the root of any project:

```bash
vouch init
```

This creates `.vouch/` with `config.yaml`, an empty `audit.log.jsonl`,
the directory skeleton, and a `.gitignore` that excludes `proposed/`
and `state.db`.

```bash
ls .vouch/
# audit.log.jsonl  claims  config.yaml  decided  entities  evidence  pages  proposed  relations  sessions  sources

vouch status
# root:   /your/project/.vouch
# counts: claims=0 pages=0 entities=0 sources=0
# pending: 0
```

Commit it:

```bash
git add .vouch && git commit -m "kb: init"
```

## 3. Register a source

Sources are what claims cite. They can be files, URLs, transcripts —
anything content-addressable.

```bash
vouch source add docs/decisions/2026-04-meeting.md \
  --title "Auth decision meeting, April 2026"
# registered source 3e2f1b... (file)
```

Re-running with the same file is a no-op — sources dedupe by content
hash.

## 4. Propose a claim

```bash
vouch propose-claim \
  --text "Auth uses JWTs in the Authorization header." \
  --source 3e2f1b... \
  --type decision \
  --confidence 0.95
# proposed: prop-abc123 (pending)
```

The proposal lives in `.vouch/proposed/prop-abc123.yaml`. It is *not*
committed (gitignored).

```bash
vouch pending
# prop-abc123  claim   you                "Auth uses JWTs in the Authorization header."

vouch show prop-abc123
# (full proposal details)
```

## 5. Approve it

```bash
vouch approve prop-abc123 --reason "matches the meeting notes"
# approved → .vouch/claims/auth-uses-jwt.yaml
```

The claim is now durable. The proposal moves to
`.vouch/decided/prop-abc123.yaml` (committed, for audit).

**Note on approval:** By default, vouch requires human approval and
prevents self-approval (a proposer cannot approve their own proposal).
For local testing, you can add `approver_role: trusted-agent` to 
`.vouch/config.yaml`. Production deployments should keep the default
`require_human_approval: true` to preserve the review gate.

```bash
git add .vouch && git commit -m "kb: approve auth-uses-jwt"
```

## 6. Wire an agent

`vouch serve` is a stdio MCP server, so the agent's native registration is all
you need:

```bash
claude mcp add vouch -- vouch serve    # or: codex mcp add vouch -- vouch serve
```

Add `-e VOUCH_AGENT=claude-code` to attribute the agent's proposals to it
rather than your shell user. Confirm with `claude mcp list` (look for
`vouch … ✓ Connected`).

Prefer a config file, or want the brain-first `CLAUDE.md`, slash commands, and
hooks too? Run `vouch install-mcp claude-code` — or drop this into `.mcp.json`
at the project root by hand:

```json
{
  "mcpServers": {
    "vouch": {
      "command": "vouch",
      "args": ["serve"],
      "env": {"VOUCH_AGENT": "claude-code"}
    }
  }
}
```

Open Claude Code in the project. It can now call `kb_search`,
`kb_propose_claim`, etc.

## 7. Read approved artifacts

Once you've approved claims and pages, read them with:

```bash
vouch read-claim auth-uses-jwt      # read an approved claim
vouch read-page <page-id>           # read an approved page
vouch read-entity <entity-id>       # read an approved entity
vouch read-relation <relation-id>   # read an approved relation
```

All of these methods are also available over the MCP and JSONL servers
(`kb.read_claim`, `kb.read_page`, etc.) for agent integration.

## 8. Lint and verify

Run periodically:

```bash
vouch lint                 # quick user-actionable issues
vouch doctor               # full sweep: source hashes, dangling refs, drift
```

CI-friendly: both exit non-zero on issues; pipe to `jq` for
machine-readable output.

## Where next?

- Wire your specific host: [adapters/](../adapters/)
- Understand the object model: [object-model.md](object-model.md)
- Read the spec if you're writing an alternative server:
  [../SPEC.md](../SPEC.md)
