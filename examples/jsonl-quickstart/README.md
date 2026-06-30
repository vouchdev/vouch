# JSONL transport quickstart

The shortest adapter contract for a new host: discovery -> status -> search
-> context over the newline-delimited JSON transport. Directly mirrors AKBP's
`jsonl-quickstart`. It seeds one approved claim, then drives a single request
sequence through `vouch serve --transport jsonl` and prints the response
envelopes.

## Run it

```bash
./examples/jsonl-quickstart/run.sh
```

The example is self-contained: it builds a throwaway KB in a `mktemp -d`
directory, runs against it, and cleans up on exit. To verify against a
specific binary, override `VOUCH`:

```bash
VOUCH=/path/to/vouch ./examples/jsonl-quickstart/run.sh
```

## What it does

1. `vouch init` creates a `.vouch/` KB and seeds one approved starter claim,
   then `cd`s into it so the JSONL server discovers the root from the cwd.
2. Builds a `requests.jsonl` with four envelopes — one per method — in the
   order a fresh adapter should implement them:

   ```text
   {"id":"caps","method":"kb.capabilities","params":{}}
   {"id":"status","method":"kb.status","params":{}}
   {"id":"search","method":"kb.search","params":{"query":"agent","limit":5}}
   {"id":"context","method":"kb.context","params":{"task":"what is this kb about","limit":5}}
   ```

3. Pipes the sequence into `vouch serve --transport jsonl` and `tee`s the
   response envelopes.
4. Asserts each envelope is `ok:true`, that `capabilities.methods` carries the
   full 54-method surface (including the four exercised here), that status
   reports the seeded claim, that search recalls it, and that the context pack
   is non-empty. Prints AKBP-style success markers.

The request/response envelope is the whole contract a host needs to mirror:

```text
request   {"id": "...", "method": "kb.<name>", "params": {...}}
success   {"id": "...", "ok": true,  "result": {...}}
failure   {"id": "...", "ok": false, "error": {"code": "...", "message": "..."}}
```

This is the minimal "read path" an adapter wires up first — discover the
method surface, check the KB is alive, retrieve, and assemble a context pack —
before it ever exposes the review-gated write methods.

## Expected output

```text
== vouch JSONL quickstart example ==

-- response envelopes (responses.jsonl) --
{"id": "caps", "ok": true, "result": {"name": "vouch", "version": "1.0.0", ... "methods": ["kb.capabilities", "kb.status", ... 54 total ...], ...}}
{"id": "status", "ok": true, "result": {"claims": 1, "pages": 1, "sources": 1, "pending_proposals": 0, "index_present": true, ...}}
{"id": "search", "ok": true, "result": {"backend": "fts5", "hits": [{"kind": "claim", "id": "vouch-starter-reviewed-knowledge", ...}], ...}}
{"id": "context", "ok": true, "result": {"query": "what is this kb about", "items": [{"id": "edit-in-obsidian", "type": "page", ...}], ...}}

-- assertions --
capability discovery ok (54 methods)
status ok (1 claim(s), 0 pending)
search ok (1 hit(s) for 'agent')
context ok (1 item(s) in pack)

== vouch JSONL quickstart example passed ==
```

## Methods demonstrated

- `kb.capabilities` — discover the method surface, transports, and feature flags.
- `kb.status` — artifact counts, pending-proposal count, index presence.
- `kb.search` — FTS5 / substring retrieval over approved artifacts.
- `kb.context` — assemble a `ContextPack` ready to inject into an agent prompt.
