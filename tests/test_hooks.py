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


def test_raw_non_json_stdin_is_tolerated(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_hit(monkeypatch)
    out = hooks.build_claude_prompt_hook(store, "when do deploys run")
    assert "tuesdays" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_no_hits_injects_nothing(store: KBStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    monkeypatch.setattr(context.index_db, "search", lambda *a, **k: [])
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "zzznomatch"})) == ""


def test_conversational_prompt_with_no_informative_tokens_injects_nothing(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt made of stopwords must inject zero tokens of context.

    Regression for the «one»/«better» noise: FTS ORs every prompt token, so
    conversational prompts matched claims on words like "one" and filled the
    block with irrelevant snippets on every turn.
    """
    monkeypatch.setattr(
        context.index_db,
        "search",
        lambda *a, **k: [("claim", "c1", "deploys run on «one» tuesday", 2.0)],
    )
    monkeypatch.setattr(context.index_db, "search_semantic", lambda *a, **k: [])
    out = hooks.build_claude_prompt_hook(
        store, json.dumps({"prompt": "honestly think, which one is better you think?"})
    )
    assert out == ""


def test_stopwords_are_stripped_from_the_retrieval_query(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def _spy(*a: object, **k: object) -> dict[str, list[object]]:
        seen["query"] = str(k["query"])
        return {"items": []}

    monkeypatch.setattr(hooks, "build_context_pack", _spy)
    hooks.build_claude_prompt_hook(
        store, json.dumps({"prompt": "when do the deploys run in ci?"})
    )
    assert seen["query"] == "deploys run ci"


def test_claim_hits_render_full_text_not_snippet_windows(store: KBStore) -> None:
    """The injected block carries the whole approved claim, not the FTS5
    16-token «»-highlighted window — elided snippets are a search-UI
    affordance and read as garbage in model context."""
    long_text = (
        "the release workflow pushes a version tag, waits for ci to go green, "
        "and then publishes the wheel to pypi with trusted publishing enabled"
    )
    src = store.put_source(b"more evidence")
    store.put_claim(Claim(id="c9", text=long_text, evidence=[src.id]))
    health.rebuild_index(store)
    out = hooks.build_claude_prompt_hook(
        store, json.dumps({"prompt": "how does the release workflow publish to pypi"})
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert long_text in ctx
    assert "«" not in ctx and "»" not in ctx


def test_non_dict_json_payload_is_safe(store: KBStore) -> None:
    for raw in ("null", "42", "true", "[1,2,3]", '"a string"'):
        assert hooks.build_claude_prompt_hook(store, raw) == ""


def test_build_context_pack_exception_is_swallowed(
    store: KBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "build_context_pack", _boom)
    # an informative prompt, so the call actually reaches build_context_pack
    # (a stopword-only prompt would short-circuit before the exception path).
    assert hooks.build_claude_prompt_hook(store, json.dumps({"prompt": "deploys"})) == ""


def test_context_hook_cli_always_exits_zero_without_kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(tmp_path)  # no .vouch here
    result = CliRunner().invoke(cli, ["context-hook"], input='{"prompt":"anything"}')
    assert result.exit_code == 0


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
