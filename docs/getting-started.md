# Getting started

In ten minutes you'll have a vouch KB at your project root, one
approved claim, and an MCP-wired agent that can read and propose to it.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '/path/to/vouch[dev]'
```

(From PyPI, published as `vouch-kb`; the CLI command is still `vouch`:
`pipx install vouch-kb`.)

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

```bash
git add .vouch && git commit -m "kb: approve auth-uses-jwt"
```

## 6. Wire an agent

Drop this into `.mcp.json` at the project root:

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

## 7. Lint and verify

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
