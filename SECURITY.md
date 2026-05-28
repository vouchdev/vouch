# Security Policy

## Supported versions

Pre-1.0, only the latest release on `main` gets security fixes. After 1.0,
this section will list a support window.

## Reporting a vulnerability

**Do not open a public issue.** Use one of:

1. **GitHub Security Advisories** (preferred):
   <https://github.com/vouchdev/vouch/security/advisories/new>
2. Email the maintainers (placeholder: `security@vouch.invalid`).

Please include:
- vouch version (`vouch --version`) and Python version.
- A minimal reproduction, ideally a failing test or a transcript of
  `vouch serve --transport jsonl` input/output.
- Impact assessment from your perspective.

We aim to acknowledge within 72 hours and to have an initial assessment
within one week. We'll coordinate disclosure timing with you.

## Threat model — what vouch defends against

vouch is designed around the assumption that **the agent is not trusted**.
The whole point of the review gate is that an LLM may hallucinate, be
prompt-injected, or be operated by someone whose interests don't match
yours. Specifically:

- **Agent → KB writes are gated.** Every `kb.propose_*` call lands in
  `proposed/` (gitignored), never directly in committed artifacts. A
  human (or trusted approver) runs `vouch approve` to promote.
- **Citations are required.** A claim without a Source or Evidence id
  fails validation. This raises the cost of fabricated facts.
- **Sources are content-hashed.** Re-registering the same bytes yields
  the same id; tampering changes the hash. `vouch source verify` and
  `vouch doctor` re-hash on demand.
- **Audit log is append-only.** Every mutation — proposal, decision,
  lifecycle change — emits an `AuditEvent` to `audit.log.jsonl`. The
  log is committed; tampering shows up in `git log -p`.
- **Bundles are signed by hash.** `manifest.json` carries a sha256 for
  every file. `vouch export-check` and `vouch import-check` verify
  before any destructive operation.

## Threat model — what vouch does *not* defend against

Be aware of these gaps:

- **Compromise of an approver.** If the human running `vouch approve`
  has been social-engineered, the gate does nothing. The audit log
  records *who* approved, which is the only post-hoc lever.
- **Malicious YAML/markdown content.** vouch parses with `yaml.safe_load`
  but downstream renderers (your editor, your wiki) may interpret
  embedded HTML, links, or images. Treat KB contents like any other
  reviewed text.
- **Storage-layer attacks.** `state.db` is a derivable cache; rebuild
  with `vouch index` if you suspect corruption. The source of truth is
  the files on disk plus git.
- **Network adversaries.** vouch has no network surface of its own — it
  speaks stdio (MCP) and stdin/stdout (JSONL). If you put it behind a
  network listener, that's your transport and your security boundary.
- **Supply chain of agent input.** If an agent ingests a poisoned web
  page and registers it as a Source, the source hash will pin it but the
  content is still poisoned. Review what gets registered.

## Hardening tips

- Set `VOUCH_AGENT` distinctly per agent so the audit log attributes
  writes correctly in multi-agent setups.
- Treat `vouch approve` as a privileged operation — don't wire it to a
  bot without a second human in the loop unless you've thought through
  what that bot can be tricked into doing.
- Commit `.vouch/audit.log.jsonl` on every change. Rotation is fine
  (compress old segments), deletion is not.
- Periodically run `vouch doctor` in CI — it re-hashes sources and
  flags drift.
