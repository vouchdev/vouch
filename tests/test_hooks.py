"""The per-prompt hook injects relevant KB context with zero tool calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vouch import context, health, hooks, salience
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    s = KBStore.init(tmp_path)
    src = s.put_source(b"e")
    s.put_claim(Claim(id="c1", text="deploys run on tuesdays via ci", evidence=[src.id]))
    health.rebuild_index(s)
    return s


def _force_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        context.index_db,
        "search_semantic",
        lambda *a, **k: [("claim", "c1", "deploys run on tuesdays via ci", 0.9)],
    )
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])


def test_empty_prompt_injects_nothing(store: KBStore) -> None:
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": ""})) == ""
    assert hooks.build_claude_prompt_hook(store, "") == ""


def test_relevant_prompt_yields_additional_context(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    env = json.loads(out)
    assert env["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "tuesdays" in env["hookSpecificOutput"]["additionalContext"]


def test_relevant_prompt_banner_instructs_from_vouch_memory(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block is instructional: the model is told to open its reply with
    "From vouch memory:" and ground in the cited items — that visible opener
    is the user-facing proof recall ran."""
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[vouch memory]")
    assert 'MUST open with the exact words "From vouch memory:"' in ctx
    # recalled facts must render visually distinct from the model's own words
    assert "markdown blockquote" in ctx


def test_raw_non_json_stdin_is_tolerated(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, "when do deploys run")
    assert "tuesdays" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_no_hits_injects_explicit_nothing_banner(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty result still injects — the user must see vouch was consulted,
    not silence that looks like vouch did nothing."""
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "zzznomatch"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "found nothing relevant" in ctx
    assert '"Nothing in vouch on this."' in ctx


def _stub_pack(monkeypatch: pytest.MonkeyPatch, score: float) -> None:
    # stub the pack builder itself (module attr on hooks), not index_db: the
    # short-circuit reads the fused item score, which hybrid retrieval rescales.
    pack = {
        "items": [
            {"summary": "deploys run on tuesdays", "citations": ["ev-1"], "score": score}
        ]
    }
    monkeypatch.setattr(hooks, "build_context_pack", lambda *a, **k: pack)


def _enable_short_circuit(store: KBStore, threshold: float = 0.8) -> None:
    store.config_path.write_text(
        f"retrieval:\n  short_circuit:\n    enabled: true\n    min_confidence: {threshold}\n",
        encoding="utf-8",
    )


def test_short_circuit_banner_on_high_confidence_lookup(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_short_circuit(store)
    _stub_pack(monkeypatch, score=50.0)  # conf = 1-exp(-10) ≈ 1.0
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "high-confidence match" in ctx
    assert 'reply with ONLY "From vouch memory:"' in ctx


def test_short_circuit_never_fires_for_action_prompts(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'do work' prompt must never collapse to a memory pass-through,
    however confident the match — the action gate, not the score, keeps
    the short-circuit safe."""
    _enable_short_circuit(store)
    _stub_pack(monkeypatch, score=50.0)
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "fix the deploy pipeline"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "high-confidence match" not in ctx
    assert 'MUST open with the exact words "From vouch memory:"' in ctx


def test_short_circuit_off_by_default(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pack(monkeypatch, score=50.0)  # confident, but the knob is opt-in
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "high-confidence match" not in ctx


def test_short_circuit_respects_threshold(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_short_circuit(store, threshold=0.99)
    _stub_pack(monkeypatch, score=8.5)  # conf ≈ 0.82 < 0.99
    out = hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    assert "high-confidence match" not in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_short_circuit_cfg_is_defensive() -> None:
    default = (False, 0.8)
    assert hooks.short_circuit_cfg({}) == default
    assert hooks.short_circuit_cfg({"retrieval": None}) == default
    assert hooks.short_circuit_cfg({"retrieval": {"short_circuit": "yes"}}) == default
    assert hooks.short_circuit_cfg(
        {"retrieval": {"short_circuit": {"enabled": True, "min_confidence": "high"}}}
    ) == (True, 0.8)
    assert hooks.short_circuit_cfg(
        {"retrieval": {"short_circuit": {"enabled": True, "min_confidence": 7}}}
    ) == (True, 0.8)


def test_non_dict_json_payload_is_safe(store: KBStore) -> None:
    for raw in ("null", "42", "true", "[1,2,3]", '"a string"'):
        assert hooks.build_claude_prompt_hook(store, raw) == ""


def test_build_context_pack_exception_is_swallowed(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "build_context_pack", _boom)
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "x"})) == ""


def test_context_hook_cli_always_exits_zero_without_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(tmp_path)  # no .vouch here
    result = CliRunner().invoke(cli, ["context-hook"], input='{"prompt":"anything"}')
    assert result.exit_code == 0


def test_context_hook_cli_uses_payload_cwd(
    store: KBStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a global install the hook process may run from anywhere: the
    payload's cwd names the project whose KB recall must read."""
    from click.testing import CliRunner

    from vouch.cli import cli

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # no .vouch here — only the payload points home
    payload = json.dumps({"prompt": "when do deploys run", "cwd": str(store.root)})
    result = CliRunner().invoke(cli, ["context-hook"], input=payload)
    assert result.exit_code == 0
    assert "vouch memory" in result.output


def test_prompt_with_session_id_feeds_salience_reflex(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for #425: the hook computed context but never recorded the
    prompt into the entity-salience reflex (#223), leaving it permanently
    empty for every claude-code session. record_query must actually run."""
    _force_hit(monkeypatch)
    session_id = "sess-425-a"
    try:
        assert salience._buffered_queries(session_id) == []
        hooks.build_claude_prompt_hook(
            store,
            json.dumps({"prompt": "when do deploys run", "session_id": session_id}),
        )
        assert salience._buffered_queries(session_id) == ["when do deploys run"]
    finally:
        salience.reset_session(session_id)


def test_prompt_without_session_id_does_not_touch_salience(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session_id in the payload -- e.g. an older host or a bare prompt
    string -- must not raise and must not create any buffer."""
    _force_hit(monkeypatch)
    hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "when do deploys run"}))
    hooks.build_claude_prompt_hook(store, "when do deploys run")
    # no session_id was ever given, so nothing should have been buffered
    # under any of the plain-string forms a caller might mistakenly pass.
    assert salience._buffered_queries("") == []
    assert salience._buffered_queries("when do deploys run") == []


def test_salience_recording_failure_does_not_block_the_hook(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Salience is a best-effort reflex; if config reading or recording
    breaks for any reason, the hook must still return real context rather
    than swallowing the whole response."""
    _force_hit(monkeypatch)

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks.salience_mod, "record_query", _boom)
    session_id = "sess-425-b"
    try:
        out = hooks.build_claude_prompt_hook(
            store,
            json.dumps({"prompt": "when do deploys run", "session_id": session_id}),
        )
        # salience recording failed, but real context must still come back.
        assert "tuesdays" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    finally:
        salience.reset_session(session_id)


# --- the prompt gate: which prompts are entitled to a turn ----------------


def _enable_gate(store: KBStore) -> None:
    import yaml

    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg.setdefault("retrieval", {})["prompt_gate"] = {"enabled": True}
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _disable_gate(store: KBStore) -> None:
    import yaml

    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    retrieval = cfg.setdefault("retrieval", {})
    retrieval.pop("prompt_gate", None)
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _hook(store: KBStore, prompt: str) -> str:
    return hooks.build_claude_prompt_hook(store, json.dumps({"prompt": prompt}))


@pytest.mark.parametrize(
    "prompt",
    [
        "fix the failing auth test",
        "please refactor storage.py",
        "ok now add a test for adopt",
        "can you run the suite and commit?",
        "bump the version",
        "continue",
        "go ahead",
    ],
)
def test_action_prompts_are_classified_as_work(prompt: str) -> None:
    assert hooks._looks_like_action(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "what is our release cutoff?",
        "how does the review gate work?",
        "why did the container job fail?",
        "can you tell me what the deploy cadence is?",
        "who owns the deploy?",
        "remind me how adopt works",
        "the tests are failing, any idea why?",
    ],
)
def test_lookup_prompts_are_not_classified_as_work(prompt: str) -> None:
    assert hooks._looks_like_action(prompt) is False


def test_work_prompt_with_a_miss_injects_nothing(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a coding command the KB knows nothing about must not
    spend the turn's opener announcing an empty search."""
    _enable_gate(store)
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    assert _hook(store, "fix the flaky websocket reconnect") == ""


def test_work_prompt_with_a_hit_is_advisory_not_a_contract(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recall still helps the work — it just must not force the reply shape."""
    _enable_gate(store)
    _force_hit(monkeypatch)
    out = _hook(store, "fix the tuesday deploy script")
    assert out  # context is still injected
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "deploys run on tuesdays" in block
    assert "work request" in block
    assert "Do NOT open your reply with a memory banner" in block
    assert "MUST open with" not in block
    assert "From vouch memory:" not in block


def test_lookup_prompt_keeps_the_visible_recall_contract(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(store)
    _force_hit(monkeypatch)
    out = _hook(store, "what day do deploys run?")
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert 'MUST open with the exact words "From vouch memory:"' in block
    assert "work request" not in block


def test_lookup_prompt_with_a_miss_still_says_so(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question the KB cannot answer keeps the honest 'nothing' answer —
    silence there is indistinguishable from vouch being broken."""
    _enable_gate(store)
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    out = _hook(store, "what is our incident escalation policy?")
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert '"Nothing in vouch on this."' in block


@pytest.mark.parametrize("prompt", ["ok thanks", "yes", "which one is better?", "hmm"])
def test_chatter_injects_nothing(
    store: KBStore, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    """Retrieval ORs every token, so 'which one' used to match on 'one'."""
    _enable_gate(store)
    _force_hit(monkeypatch)
    assert _hook(store, prompt) == ""


def test_work_prompts_get_a_smaller_pack(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recall should inform the work, not crowd the turn it is helping."""
    _enable_gate(store)
    _force_hit(monkeypatch)
    seen: dict[str, int] = {}

    real = context.build_context_pack

    def _spy(store_arg, **kwargs):
        seen["limit"] = kwargs.get("limit")
        seen["max_chars"] = kwargs.get("max_chars")
        return real(store_arg, **kwargs)

    monkeypatch.setattr(hooks, "build_context_pack", _spy)
    _hook(store, "fix the tuesday deploy script")
    assert seen["limit"] == hooks._WORK_MAX_ITEMS
    assert seen["max_chars"] == hooks._WORK_MAX_CHARS
    _hook(store, "what day do deploys run?")
    assert seen["limit"] == hooks._MAX_ITEMS
    assert seen["max_chars"] == hooks._MAX_CHARS


def test_gate_absent_keeps_todays_behaviour(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing KBs have no prompt_gate key on disk and must not change."""
    _disable_gate(store)
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    out = _hook(store, "fix the flaky websocket reconnect")
    block = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert '"Nothing in vouch on this."' in block
    # chatter, too: unchanged without the key
    assert _hook(store, "ok thanks") != ""


def test_starter_config_ships_the_gate_on(tmp_path: Path) -> None:
    import yaml

    fresh = KBStore.init(tmp_path / "fresh")
    cfg = yaml.safe_load(fresh.config_path.read_text(encoding="utf-8"))
    assert cfg["retrieval"]["prompt_gate"]["enabled"] is True
    assert hooks.prompt_gate_cfg(cfg) is True


def test_prompt_gate_cfg_is_defensive() -> None:
    assert hooks.prompt_gate_cfg({}) is False
    assert hooks.prompt_gate_cfg({"retrieval": "nonsense"}) is False
    assert hooks.prompt_gate_cfg({"retrieval": {"prompt_gate": []}}) is False
    assert hooks.prompt_gate_cfg({"retrieval": {"prompt_gate": {}}}) is False
