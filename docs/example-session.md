# Example session

![vouch end-to-end demo](demo.gif)

A full propose → review → commit → retrieve loop, captured from a real
run on 2026-05-21. Reproduce by following along in any git repo, or
re-render the GIF with `vhs docs/demo.tape` (see [demo.tape](demo.tape)).

## Setup

```bash
$ mkdir demo && cd demo && git init -q
$ echo "Authentication uses stateless JWTs signed with RS256." > auth.md
$ git add -A && git commit -q -m "init"
$ vouch init
Initialised KB at /tmp/demo/.vouch
Next: `vouch serve` to expose the MCP server to your agent.
```

`vouch init` creates the `.vouch/` directory with empty subfolders for
claims, pages, entities, relations, sources, sessions, proposed,
decided, plus `audit.log.jsonl` and `state.db`.

## 1. Register a source

The agent (or you) registers the file as evidence material. Sources
are content-addressed — the id is the sha256 of the file content, so
the same file registered twice de-duplicates.

```bash
$ vouch source add auth.md --title "auth notes"
816fec5eb02e8965df3197cdd622c394c8845364c584fe0fe0023dd0459e8982
```

## 2. Propose a claim

Agents call `kb.propose_claim` (over MCP/JSONL); from the CLI it looks
like this. The `VOUCH_AGENT` env var records *which* agent proposed,
so multi-agent setups stay attributable.

```bash
$ VOUCH_AGENT=claude-code vouch propose-claim \
    --text "Authentication uses stateless JWTs signed with RS256" \
    --source 816fec5e... \
    --type fact \
    --confidence 0.9
20260521-065702-44a92aa8
```

The proposal lands in `.vouch/proposed/<id>.yaml` (gitignored — it
shouldn't pollute history until you approve it).

## 3. Review the queue

```bash
$ vouch pending
• 20260521-065702-44a92aa8  [claim]  by claude-code
    Authentication uses stateless JWTs signed with RS256

$ vouch show 20260521-065702-44a92aa8
id: 20260521-065702-44a92aa8
kind: claim
proposed_by: claude-code
proposed_at: '2026-05-21T06:57:02.910715Z'
payload:
  id: authentication-uses-stateless-jwts-signed-with-rs256
  text: Authentication uses stateless JWTs signed with RS256
  type: fact
  confidence: 0.9
  evidence:
  - 816fec5eb02e8965df3197cdd622c394c8845364c584fe0fe0023dd0459e8982
status: pending
```

## 4. Approve

```bash
$ vouch approve 20260521-065702-44a92aa8 --reason "matches the code"
Approved → claim/authentication-uses-stateless-jwts-signed-with-rs256
```

What just happened (see [the approve flow](../spec/review-gate.md) for the
formal version):

1. A durable artifact is written to `.vouch/claims/<slug>.yaml` with
   `approved_by` stamped on it.
2. The FTS5 index in `state.db` is updated so the claim is searchable
   immediately.
3. The proposal file moves from `proposed/` → `decided/` with
   `status=approved`, `decided_by`, `decision_reason`.
4. An `audit.log.jsonl` line records the decision.

```bash
$ vouch status
KB at /tmp/demo/.vouch
  durable: 1 claims  •  0 pages  •  1 sources  •  0 entities  •  0 relations
  pending: 0 proposals
  audit:   4 events  •  index: present
```

The on-disk claim:

```bash
$ cat .vouch/claims/authentication-uses-stateless-jwts-signed-with-rs256.yaml
id: authentication-uses-stateless-jwts-signed-with-rs256
text: Authentication uses stateless JWTs signed with RS256
type: fact
status: working
confidence: 0.9
evidence:
- 816fec5eb02e8965df3197cdd622c394c8845364c584fe0fe0023dd0459e8982
scope: project
created_at: '2026-05-21T06:57:13.947450Z'
updated_at: '2026-05-21T06:57:13.947477Z'
approved_by: claude-code
```

## 5. Retrieve

`vouch search` is for ranked snippets; `vouch context` builds a
prompt-ready bundle with a quality gate.

```bash
$ vouch search "JWT"
[claim] authentication-uses-stateless-jwts-signed-with-rs256  score=1.000  (substring)
    Authentication uses stateless JWTs signed with RS256

$ vouch context "JWT"
{
  "query": "JWT",
  "items": [
    {
      "id": "authentication-uses-stateless-jwts-signed-with-rs256",
      "type": "claim",
      "summary": "Authentication uses stateless JWTs signed with RS256",
      "score": 1.0,
      "backend": "substring",
      "citations": ["816fec5eb02e8965df3197cdd622c394c8845364c584fe0fe0023dd0459e8982"],
      "freshness": "unknown"
    }
  ],
  "quality": { "ok": true, "items": 1, "warnings": 0, "uncited_items": [] }
}
```

> **Note.** The default search backend is literal (FTS5 + substring),
> so a query like `"how does auth work"` won't match `"Authentication
> uses..."`. Install the embeddings extra (`pip install -e
> '.[embeddings-mpnet]'`) for semantic matching.

## 6. Audit trail

```bash
$ vouch audit --tail 5
2026-05-21T06:56:54Z  kb.init                  by claude-code  objects=[]
2026-05-21T06:56:58Z  source.add               by claude-code  objects=['816fec5e...']
2026-05-21T06:57:02Z  proposal.claim.create    by claude-code  objects=['20260521-065702-44a92aa8']
2026-05-21T06:57:14Z  proposal.claim.approve   by claude-code  objects=['20260521-065702-44a92aa8', 'authentication-uses-stateless-jwts-signed-with-rs256']
```

Every mutation is on the record, with actor and object ids.

## 7. Commit

```bash
$ git add .vouch/ && git commit -m "kb: approve auth-uses-jwt"
```

What lands in git: the durable artifact, the decision record, the
audit line. What doesn't: the `proposed/` draft (gitignored, since
unreviewed agent output shouldn't pollute history) and `state.db` (a
derivable cache — `vouch index` rebuilds it).

## Next steps

- Wire vouch into Claude Code via `.mcp.json` (see [README](../README.md#wiring-into-claude-code)).
- Open a session for a longer task: `vouch session start --task "build the deploy pipeline"`, then `vouch crystallize <id>` at the end to promote the session's work into proposals.
- Export the KB as a portable bundle: `vouch export --out kb.tar.gz`.
