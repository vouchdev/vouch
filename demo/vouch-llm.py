#!/usr/bin/env python3
"""Minimal Anthropic Messages shim for the vouch demo image.

vouch's LLM-backed features (page compile, session summaries) don't call an
API directly — they shell out to a deployment-configured command
(`compile.llm_cmd` in .vouch/config.yaml) with the prompt on stdin, and read
the model's reply from stdout. In a normal install that command is the local
`claude` CLI. The demo image has no CLI and no baked-in key, so this shim is
the `llm_cmd`: it reads the prompt on stdin and calls the Anthropic Messages
API using a key the *user* supplies via ANTHROPIC_API_KEY.

Stdlib only (urllib) — no extra pip dependency, mirroring vouch's own client
in src/vouch/pr_cache.py. Emits only the model's text on stdout so vouch's
`parse_drafts` sees a clean JSON array; all diagnostics go to stderr, and a
non-zero exit lets vouch surface a clean "compile.llm_cmd failed" message.

Env:
  ANTHROPIC_API_KEY    required — user's key; absent => exit 3, features off.
  ANTHROPIC_MODEL      default claude-sonnet-4-5 (override for a newer Sonnet).
  ANTHROPIC_BASE_URL   default https://api.anthropic.com
  ANTHROPIC_MAX_TOKENS default 8192 (compile/split return multi-page JSON).
  ANTHROPIC_TIMEOUT    default 150 (seconds; below vouch's own subprocess cap).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        sys.stderr.write(
            "ANTHROPIC_API_KEY is not set — this demo's LLM features "
            "(page compile, session summaries) are disabled. Set the key and "
            "restart to enable Claude.\n"
        )
        return 3

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip()
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    try:
        max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8192"))
        timeout = float(os.environ.get("ANTHROPIC_TIMEOUT", "150"))
    except ValueError as e:
        sys.stderr.write(f"invalid ANTHROPIC_MAX_TOKENS/ANTHROPIC_TIMEOUT: {e}\n")
        return 2

    prompt = sys.stdin.read()
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = (e.read().decode("utf-8", "replace") or "").strip()[:400]
        sys.stderr.write(f"anthropic API {e.code}: {detail}\n")
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        sys.stderr.write(f"anthropic API call failed: {e}\n")
        return 1

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        sys.stderr.write(f"anthropic API returned non-JSON: {body[:200]!r}\n")
        return 1

    text = "".join(
        block.get("text", "")
        for block in (data.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text.strip():
        sys.stderr.write(f"anthropic API returned no text content: {body[:200]!r}\n")
        return 1

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
