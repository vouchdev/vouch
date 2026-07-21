"""Live compliance probe for the model-delegated prompt gate.

The prompt gate hands the host model ONE conditional instruction and trusts it
to choose loud recall (a question), silent background (a task), or ignore. Unit
tests pin the injected *block*; this probe measures whether a real model
actually *obeys* it — the half a unit test cannot cover.

Skipped by default (needs a live, authenticated `claude` CLI, so it is useless
in headless CI, exactly like ``test_openclaw_plugin_load_real``). Opt in:

    VOUCH_LIVE_EVAL=1 pytest tests/test_prompt_gate_live.py -v

Measured once on 2026-07-21 (real `claude -p`, KB-backed block, 3 tiers):
tasks were never wrongly announced on any tier; questions were announced by
every tier once the block came from a real KB (not a hand-crafted string).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vouch import hooks
from vouch.models import Claim
from vouch.storage import KBStore

pytestmark = pytest.mark.skipif(
    os.environ.get("VOUCH_LIVE_EVAL") != "1" or shutil.which("claude") is None,
    reason="live probe: set VOUCH_LIVE_EVAL=1 with an authenticated `claude` CLI",
)

MODEL = os.environ.get("VOUCH_EVAL_MODEL", "haiku")

# (prompt, expected) — banner | no_banner | nothing
CASES = [
    ("what day do deploys run?", "banner"),
    ("how often does staging refresh?", "banner"),
    ("fix the deploy script for the tuesday release", "no_banner"),
    ("vendorize the deploy dependencies", "no_banner"),
    ("what is our incident escalation policy?", "nothing"),
    ("fix the flaky websocket reconnect timeout", "no_banner"),
]


def _seed(store: KBStore) -> None:
    facts = [
        "deploys run every second tuesday",
        "the staging environment refreshes nightly at 02:00 utc",
        "rollbacks use the blue-green switch, never a redeploy of an old tag",
    ]
    for i, text in enumerate(facts):
        src = store.put_source(text.encode())
        store.put_claim(Claim(id=f"f{i}", text=text, evidence=[src.id]))


def _block(store: KBStore, prompt: str) -> str:
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": prompt}))
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _ask_model(block: str, prompt: str, workdir: Path) -> str:
    payload = f"{block}\n\n{prompt}" if block else prompt
    res = subprocess.run(
        ["claude", "-p", "--settings", '{"hooks":{}}', "--model", MODEL,
         "--disallowed-tools", "Bash", "Read", "Edit", "Write", "Glob",
         "Grep", "WebFetch", "WebSearch", "Task"],
        input=payload, capture_output=True, text=True, timeout=180, cwd=str(workdir),
    )
    return (res.stdout or "").strip()


def _obeys(expected: str, reply: str) -> bool:
    head = reply.strip().lower()
    banner = head.startswith("from vouch memory")
    if expected == "banner":
        return banner
    if expected == "no_banner":
        return not banner
    if expected == "nothing":
        return "nothing in vouch" in head[:200]
    return False


def test_model_obeys_the_conditional_block(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "kb")
    _seed(store)
    failures: list[str] = []
    for prompt, expected in CASES:
        reply = _ask_model(_block(store, prompt), prompt, tmp_path)
        if not _obeys(expected, reply):
            failures.append(f"{expected!r} for {prompt!r}: {reply[:100]!r}")
    # A task must NEVER be wrongly announced — that is the whole point, and it
    # held on every tier in the offline eval. Questions may occasionally
    # under-announce on a weaker tier; allow one soft miss there.
    task_fails = [f for f in failures if "no_banner" in f]
    assert not task_fails, f"tasks wrongly announced ({MODEL}): {task_fails}"
    assert len(failures) <= 1, f"too many misses on {MODEL}: {failures}"
