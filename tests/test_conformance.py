"""Conformance suite — assert the vouch reference server passes and a
deliberately-broken server fails with clear output.

Most checks run against an `InProcessClient` so the suite is fast and
deterministic. One end-to-end test spawns `vouch serve --transport jsonl`
as a subprocess to confirm the JSONL transport path still works.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from vouch import conformance as conf
from vouch.cli import cli
from vouch.storage import KBStore


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    """Initialise a throwaway KB with `trusted-agent` review policy."""
    store = KBStore.init(tmp_path)
    # trusted-agent so mutating checks can approve as the same agent that
    # proposed without tripping the self-approval gate.
    cfg = tmp_path / ".vouch" / "config.yaml"
    parsed = yaml.safe_load(cfg.read_text()) or {}
    parsed.setdefault("review", {})["approver_role"] = "trusted-agent"
    cfg.write_text(yaml.safe_dump(parsed))
    monkeypatch.chdir(store.root)
    monkeypatch.setenv("VOUCH_AGENT", "conformance-test")
    return store


# --- compliant server passes ---------------------------------------------


def test_reference_server_passes_readonly_checks(kb: KBStore) -> None:
    """The in-tree vouch server must pass every read-only check."""
    client = conf.InProcessClient()
    report = conf.run_suite(client, mutating=False)
    failed = [r for r in report.results if r.status == "fail"]
    assert not failed, "\n".join(f"{r.name}: {r.message}" for r in failed)
    assert report.ok
    assert report.counts["pass"] >= 10


def test_reference_server_passes_mutating_checks(kb: KBStore) -> None:
    """End-to-end: propose → approve → durable, plus reject → no artifact."""
    client = conf.InProcessClient()
    report = conf.run_suite(client, mutating=True)
    failed = [r for r in report.results if r.status == "fail"]
    assert not failed, "\n".join(f"{r.name}: {r.message}" for r in failed)
    assert report.ok
    # All four mutating checks must run; skips here would mean the
    # review-gate path is silently disabled.
    mutating_results = [r for r in report.results if r.category == "write"]
    assert len(mutating_results) == 4
    assert all(r.status == "pass" for r in mutating_results), [
        (r.name, r.status, r.message) for r in mutating_results
    ]


# --- deliberately-broken servers fail with clear output ------------------


class _BrokenClient:
    """Wraps the reference client and rewrites selected responses.

    Used to prove the suite catches real divergence from the spec instead
    of just nodding at whatever the server says.
    """

    def __init__(self, rewrites: dict[str, Any]) -> None:
        """Wrap the reference client and apply method -> response rewrites."""
        self._inner = conf.InProcessClient()
        self._rewrites = rewrites

    def call(self, method: str, params: dict | None = None) -> dict:
        """Return a rewritten response if `method` is overridden, else passthrough."""
        if method in self._rewrites:
            return self._rewrites[method]
        return self._inner.call(method, params)

    def close(self) -> None:
        """Close the wrapped client."""
        self._inner.close()


def test_broken_server_review_gated_false_fails(kb: KBStore) -> None:
    """SPEC §9: review_gated must be true. Anything else is a fail."""
    client = _BrokenClient({
        "kb.capabilities": {
            "id": "x", "ok": True,
            "result": {
                "version": "0.0.1", "methods": list(conf.SPEC_METHODS_ALL),
                "retrieval": ["fts5"], "transports": ["jsonl"],
                "review_gated": False,
            },
        },
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    failures = {r.name: r.message for r in report.results if r.status == "fail"}
    assert "capabilities.review_gated" in failures
    assert "review_gated MUST be true" in failures["capabilities.review_gated"]


def test_broken_server_omits_spec_methods_fails(kb: KBStore) -> None:
    """A server that omits SPEC §5 methods from capabilities.methods fails."""
    client = _BrokenClient({
        "kb.capabilities": {
            "id": "x", "ok": True,
            "result": {
                "version": "0.0.1", "methods": ["kb.capabilities", "kb.status"],
                "retrieval": ["fts5"], "transports": ["jsonl"],
                "review_gated": True,
            },
        },
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    failures = {r.name: r.message for r in report.results if r.status == "fail"}
    assert "capabilities.declares_spec_methods" in failures


def test_broken_server_unknown_method_silently_succeeds_fails(kb: KBStore) -> None:
    """A server that returns ok=true for unknown methods fails the suite."""
    client = _BrokenClient({
        "kb.this_method_does_not_exist": {"id": "x", "ok": True, "result": {}},
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    failures = {r.name: r.message for r in report.results if r.status == "fail"}
    assert "errors.unknown_method" in failures


def test_broken_server_missing_required_capabilities_field_fails(kb: KBStore) -> None:
    """capabilities response missing `version` etc. fails the shape check."""
    client = _BrokenClient({
        "kb.capabilities": {
            "id": "x", "ok": True,
            "result": {"methods": [], "transports": ["jsonl"], "review_gated": True},
        },
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    names = {r.name for r in report.results if r.status == "fail"}
    assert "capabilities.shape" in names


def test_broken_server_wrong_error_code_for_missing_param_fails(kb: KBStore) -> None:
    """SPEC Section 6: missing required param MUST yield `missing_param`."""
    client = _BrokenClient({
        "kb.read_claim": {
            "id": "x", "ok": False,
            "error": {"code": "invalid_request", "message": "no claim_id"},
        },
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    failures = {r.name: r.message for r in report.results if r.status == "fail"}
    assert "errors.missing_param" in failures
    assert "missing_param" in failures["errors.missing_param"]


def test_broken_server_list_returns_object_fails(kb: KBStore) -> None:
    """`kb.list_*` MUST return arrays per SPEC §5.1."""
    client = _BrokenClient({
        "kb.list_claims": {"id": "x", "ok": True, "result": {"not": "a list"}},
    })
    report = conf.run_suite(client, mutating=False)
    assert not report.ok
    failed = {r.name for r in report.results if r.status == "fail"}
    assert "kb.list_claims.is_list" in failed


# --- shape sanity --------------------------------------------------------


def test_raising_list_check_is_labelled_descriptively(kb: KBStore) -> None:
    """If a list-check closure raises, the report labels it by its method,
    not by the closure's `_do` `__name__`.
    """

    class _BoomClient:
        """Client whose every call raises, to exercise the catch-all path."""

        def call(self, method: str, params: dict | None = None) -> dict:
            """Always raise to simulate a check blowing up."""
            raise RuntimeError("boom")

        def close(self) -> None:
            """No-op."""
            pass

    report = conf.run_suite(_BoomClient(), mutating=False)
    names = {r.name for r in report.results if r.status == "fail"}
    # Every list-check closure raised; each must report its real name.
    for method in (
        "kb.list_pages", "kb.list_claims", "kb.list_entities",
        "kb.list_relations", "kb.list_sources", "kb.list_pending",
    ):
        assert f"{method}.is_list" in names, (method, names)
    assert "_do" not in names


def test_report_to_dict_round_trips(kb: KBStore) -> None:
    """`--json` output must be machine-readable and contain the result list."""
    report = conf.run_suite(conf.InProcessClient(), mutating=False)
    payload = report.to_dict()
    # JSON-serialisable
    encoded = json.dumps(payload)
    parsed = json.loads(encoded)
    assert parsed["ok"] is True
    assert "counts" in parsed
    assert parsed["counts"]["pass"] >= 10
    assert len(parsed["results"]) == len(report.results)


def test_format_report_shows_pass_and_fail(kb: KBStore) -> None:
    """format_report includes both PASS and FAIL lines plus a summary."""
    client = _BrokenClient({
        "kb.list_claims": {"id": "x", "ok": True, "result": {"not": "a list"}},
    })
    text = conf.format_report(conf.run_suite(client, mutating=False))
    assert "[PASS]" in text
    assert "[FAIL]" in text
    assert "summary:" in text
    assert "fail" in text


# --- CLI surface ---------------------------------------------------------


def test_cli_conformance_against_reference_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vouch conformance --mutating` passes against a freshly-initted KB.

    The CLI provisions its own throwaway KB for --mutating runs, so this
    exercises the subprocess transport + mutating checks end-to-end.
    """
    KBStore.init(tmp_path)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["conformance", "--mutating"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "summary:" in result.output


def test_cli_conformance_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vouch conformance --mutating --json` emits a parseable JSON report."""
    KBStore.init(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["conformance", "--mutating", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["counts"]["fail"] == 0


def test_cli_conformance_with_custom_broken_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom server that violates the spec must make the CLI exit non-zero.

    Exercises the subprocess transport end-to-end against a deliberately
    broken server, proving the acceptance criterion from the issue.
    """
    KBStore.init(tmp_path)
    monkeypatch.chdir(tmp_path)

    broken = tmp_path / "broken_server.py"
    broken.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if req['method'] == 'kb.capabilities':\n"
        "        out = {'id': req['id'], 'ok': True,\n"
        "               'result': {'version': '0.0.1', 'methods': [],\n"
        "                          'retrieval': [], 'transports': ['jsonl'],\n"
        "                          'review_gated': False}}\n"
        "    else:\n"
        "        out = {'id': req['id'], 'ok': True, 'result': {}}\n"
        "    sys.stdout.write(json.dumps(out) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )
    # SERVER_CMD is argv tokens, not a shell string, so paths with spaces
    # or backslashes don't need quoting/escaping.
    runner = CliRunner()
    result = runner.invoke(cli, ["conformance", sys.executable, str(broken)])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "review_gated" in result.output

    # Lock in option-like passthrough: tokens that look like Click flags
    # (e.g. `-c`, `-m`) MUST be forwarded to the server, not re-parsed by
    # the conformance command. The `ignore_unknown_options=True` context
    # setting and `nargs=-1` argument together guarantee this.
    inline_broken = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    req=json.loads(line)\n"
        "    sys.stdout.write(json.dumps({'id':req['id'],'ok':True,"
        "'result':{'version':'0.0.1','methods':[],'retrieval':[],"
        "'transports':['jsonl'],'review_gated':False}"
        " if req['method']=='kb.capabilities' else {}})+'\\n')\n"
        "    sys.stdout.flush()\n"
    )
    result = runner.invoke(
        cli, ["conformance", sys.executable, "-c", inline_broken],
    )
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output


# --- subprocess transport sanity -----------------------------------------


def test_subprocess_client_drives_vouch_serve_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spawning `vouch serve --transport jsonl` end-to-end returns capabilities."""
    KBStore.init(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = conf.SubprocessClient(
        [sys.executable, "-m", "vouch.cli", "serve", "--transport", "jsonl"],
        cwd=tmp_path,
    )
    try:
        resp = client.call("kb.capabilities")
        assert resp["ok"] is True
        assert resp["result"]["review_gated"] is True
    finally:
        client.close()


def test_subprocess_client_rejects_response_id_mismatch(tmp_path: Path) -> None:
    """A server that returns the wrong `id` must fail loudly, not silently desync.

    The transport's request/response pairing relies on the echoed `id` per
    SPEC Section 6; without verification two adjacent calls could be paired
    with each other's responses.
    """
    # Liar emits one mismatched-id response and exits immediately — keeps the
    # test deterministic on every platform without relying on `for line in
    # sys.stdin` seeing EOF.
    liar = tmp_path / "liar.py"
    liar.write_text(
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'id': 'wrong', 'ok': True, 'result': {}}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    client = conf.SubprocessClient(
        [sys.executable, str(liar)], cwd=tmp_path,
    )
    try:
        with pytest.raises(RuntimeError, match="response id mismatch"):
            client.call("kb.capabilities")
    finally:
        client.close()


def test_subprocess_client_call_times_out_on_hanging_server(tmp_path: Path) -> None:
    """A server that swallows input without responding must surface as TimeoutError.

    Regression for an unbounded `readline()` in `SubprocessClient.call`: a buggy
    or wedged kb.* server should fail the suite with a clear error, not hang
    indefinitely.
    """
    hanger = tmp_path / "hanger.py"
    hanger.write_text(
        "import sys, time\n"
        "for _ in sys.stdin:\n"
        "    time.sleep(60)\n"
    )
    client = conf.SubprocessClient(
        [sys.executable, str(hanger)], cwd=tmp_path, timeout=1.0,
    )
    try:
        with pytest.raises(TimeoutError, match="did not respond"):
            client.call("kb.capabilities")
    finally:
        client.close()
