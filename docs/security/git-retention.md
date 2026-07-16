# Git retention and secrets

vouch commits your knowledge base. `.vouch/` — claims, pages, and the
`audit.log.jsonl` event stream — is tracked in git on purpose: the diff-in-PRs
property and the authoritative audit history both depend on it. The cost of
that design is that anything which reaches a committed artifact is **permanent**.
Removing it in a later commit does not remove it from history; the append-only
audit log is never rewritten by hand at all. A credential that lands in a
committed claim, page, or session summary is in your git history forever unless
you rewrite that history, and must be treated as compromised.

## What vouch does to keep secrets out

Capture masks credentials **before** they reach the buffer. Every observation
that passes through `capture.observe` — Bash commands, tool summaries — is run
through `secrets.mask_secrets`, which replaces well-known credential formats
(AWS keys, GitHub/OpenAI/Anthropic/Slack/Google tokens, JWTs, private-key
blocks, and `password=`/`token=`-style assignments) with `[redacted-secret]`.
The buffer is where a pasted key would otherwise become a committed session
page and an append-only audit fact, so masking there is the load-bearing guard.

The masker is deliberately conservative. It matches curated patterns, not raw
entropy, because entropy scanning shreds ordinary high-entropy content — git
shas, uuids, base64 — and would corrupt legitimate observations. The trade-off
is that an exotic or home-grown token format can slip through. `vouch redact`
is the backstop for anything that does.

## If a secret still lands in a claim

```bash
vouch redact <claim_id>
```

`redact` masks the secret in the claim's text, marks the claim `REDACTED` (so it
drops out of live retrieval), and records a `claim.redact` audit event. It fixes
the **current working tree**. It does not, and cannot, rewrite the append-only
audit log or your git history.

| `vouch redact` does | `vouch redact` does not |
|---|---|
| rewrite the claim's stored text | rewrite past git commits |
| mark the claim `REDACTED` | rewrite the append-only audit log |
| drop the claim from retrieval | rotate the leaked credential for you |
| record a `claim.redact` event | make the secret unrecoverable from history |

## Actually removing a leaked secret

Redaction is remediation in the tree, not erasure from history. If a real
credential was committed:

1. **Rotate it first.** Assume anything that reached git is compromised — a
   pull request, a fork, or a clone may already hold it. Rotation is the only
   fix that actually closes the exposure; everything below is cleanup.
2. **Purge it from history** with `git filter-repo` (or BFG), for example
   `git filter-repo --replace-text <patterns-file>`. This rewrites every commit
   that touched the secret.
3. **Force-push the rewritten history** and have every collaborator re-clone —
   rewriting shared history is disruptive and old clones still hold the secret
   until they are replaced.

The order matters: rotate, then clean up. A purged-but-unrotated secret is still
a live credential sitting in someone's old clone.
