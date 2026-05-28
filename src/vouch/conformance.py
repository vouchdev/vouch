"""Conformance suite for `kb.*` servers.

Exercises the documented method surface from SPEC.md against any server
speaking the JSONL transport (§6), so an alternative implementation can
prove it conforms. The suite is also exposed as the `vouch conformance`
CLI command.

Design:

- `Client` is the transport abstraction — `call(method, params) -> envelope`.
  `SubprocessClient` drives an arbitrary `kb.*` server via JSONL over a
  spawned process. `InProcessClient` calls the local `handle_request()`
  directly, which keeps the suite's own tests fast and deterministic.

- A `Check` is a callable that takes a `Client` and returns a `CheckResult`
  (PASS / FAIL / SKIP with a one-line message). The registry lists checks
  in stable order; categories let `vouch conformance` filter what runs.

- `run_suite` returns a `ConformanceReport`. The CLI renders it; tests assert
  on it directly.

Read-only checks always run. Mutating checks (proposal → approve / reject)
require `--mutating`, since they require write access to the KB the server
is talking to. The CLI sets up a throwaway KB for that case so the user's
real KB is never touched.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# --- transport -----------------------------------------------------------


class Client(Protocol):
    """Minimal transport: send a request envelope, get a response envelope."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a `kb.*` request and return the response envelope."""
        ...

    def close(self) -> None:
        """Release any underlying transport resources."""
        ...


class SubprocessClient:
    """JSONL client that drives a `kb.*` server as a subprocess."""

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Spawn `cmd` and prepare JSONL pipes; `timeout` applies per call."""
        self._cmd = cmd
        self._timeout = timeout
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            text=True,
            bufsize=1,
        )

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Write a JSONL request and return the matching response envelope."""
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("subprocess pipes are closed")
        expected_id = uuid.uuid4().hex[:8]
        envelope = {"id": expected_id, "method": method, "params": params or {}}
        self._proc.stdin.write(json.dumps(envelope) + "\n")
        self._proc.stdin.flush()
        # readline() blocks indefinitely if the server hangs. select() on a
        # subprocess pipe is POSIX-only, so use a per-call reader thread —
        # portable across POSIX and Windows, and conformance is not hot-path.
        result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def _reader() -> None:
            try:
                result.put(("line", self._proc.stdout.readline()))  # type: ignore[union-attr]
            except Exception as e:
                result.put(("err", e))

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            kind, payload = result.get(timeout=self._timeout)
        except queue.Empty:
            # Tear the subprocess down so the orphaned reader thread can exit
            # (its readline returns "" on EOF) and so a subsequent .call() on
            # the same client fails clearly instead of pairing the next
            # request with a stale response.
            self._proc.kill()
            self._proc.wait()
            raise TimeoutError(
                f"server did not respond within {self._timeout}s to {method}"
            ) from None
        if kind == "err":
            raise payload
        line = payload
        if not line:
            raise RuntimeError(
                f"server closed stdout while waiting for response to {method}; "
                f"stderr: {self._drain_stderr_nonblocking()[:500]}"
            )
        resp = json.loads(line)
        # SPEC Section 6: response envelope echoes the request `id`. A
        # mismatch means the channel is desynchronised — fail loudly rather
        # than silently attribute one method's response to another.
        if resp.get("id") != expected_id:
            raise RuntimeError(
                f"response id mismatch for {method}: "
                f"expected {expected_id!r}, got {resp.get('id')!r}; "
                f"stderr: {self._drain_stderr_nonblocking()[:500]}"
            )
        return resp

    def _drain_stderr_nonblocking(self) -> str:
        """Return whatever stderr has produced *so far* without blocking.

        `subprocess.PIPE.read()` blocks until EOF — fine for a process we
        just killed, dangerous for one still running. Kill the subprocess so
        the OS closes its stderr fd; the subsequent read then returns
        immediately with whatever the process emitted before dying. Works
        on POSIX and Windows alike (no `select` on pipes on Windows).
        """
        if self._proc.stderr is None:
            return ""
        if self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=self._timeout)
            except Exception:
                return ""
        try:
            return self._proc.stderr.read() or ""
        except Exception:
            return ""

    def close(self) -> None:
        """Close stdin to ask the server to exit; kill it if it doesn't."""
        if self._proc.poll() is None:
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()


class InProcessClient:
    """Direct in-process client used by the suite's own tests.

    Wraps `handle_request` so we exercise the same code path as the
    JSONL transport without paying for subprocess startup.
    """

    def __init__(self) -> None:
        """Bind to the in-tree `handle_request` so calls skip the subprocess."""
        from .jsonl_server import handle_request

        self._handle = handle_request

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Invoke `handle_request` synchronously and return its envelope."""
        envelope = {"id": uuid.uuid4().hex[:8], "method": method, "params": params or {}}
        return self._handle(envelope)

    def close(self) -> None:  # pragma: no cover - nothing to release
        """No-op; this client owns no transport resources."""
        pass


# --- result types --------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single conformance check (pass / fail / skip + message)."""

    name: str
    category: str
    status: str  # "pass" | "fail" | "skip"
    message: str = ""

    @property
    def passed(self) -> bool:
        """True iff this check passed."""
        return self.status == "pass"


@dataclass
class ConformanceReport:
    """Aggregate of `CheckResult`s for one conformance suite run."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no check failed (skips are allowed)."""
        return all(r.status != "fail" for r in self.results)

    @property
    def counts(self) -> dict[str, int]:
        """Tally of results by status (`pass` / `fail` / `skip`)."""
        out = {"pass": 0, "fail": 0, "skip": 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serialise the report to a JSON-friendly dict."""
        return {
            "ok": self.ok,
            "counts": self.counts,
            "results": [
                {"name": r.name, "category": r.category,
                 "status": r.status, "message": r.message}
                for r in self.results
            ],
        }


# --- spec-mandated method surface ----------------------------------------


# SPEC.md §5 — methods every conforming server MUST expose.
SPEC_METHODS_READ = (
    "kb.capabilities",
    "kb.status",
    "kb.search",
    "kb.context",
    "kb.read_page",
    "kb.read_claim",
    "kb.read_entity",
    "kb.read_relation",
    "kb.list_pages",
    "kb.list_claims",
    "kb.list_entities",
    "kb.list_relations",
    "kb.list_sources",
    "kb.list_pending",
)
SPEC_METHODS_SOURCE = (
    "kb.register_source",
    "kb.register_source_from_path",
    "kb.source_verify",
)
SPEC_METHODS_WRITE = (
    "kb.propose_claim",
    "kb.propose_page",
    "kb.propose_entity",
    "kb.propose_relation",
)
SPEC_METHODS_DECIDE = ("kb.approve", "kb.reject")
SPEC_METHODS_LIFECYCLE = (
    "kb.supersede",
    "kb.contradict",
    "kb.archive",
    "kb.confirm",
    "kb.cite",
)
SPEC_METHODS_SESSIONS = (
    "kb.session_start",
    "kb.session_end",
    "kb.crystallize",
)
SPEC_METHODS_MAINTENANCE = (
    "kb.index_rebuild",
    "kb.lint",
    "kb.doctor",
    "kb.audit",
    "kb.export",
    "kb.export_check",
    "kb.import_check",
    "kb.import_apply",
)
SPEC_METHODS_ALL = (
    *SPEC_METHODS_READ,
    *SPEC_METHODS_SOURCE,
    *SPEC_METHODS_WRITE,
    *SPEC_METHODS_DECIDE,
    *SPEC_METHODS_LIFECYCLE,
    *SPEC_METHODS_SESSIONS,
    *SPEC_METHODS_MAINTENANCE,
)

# Error codes the SPEC mandates the JSONL transport return (§6).
SPEC_ERROR_CODES = {"method_not_found", "missing_param", "invalid_request", "internal_error"}

# Transport identifier this suite drives. The Client implementations in this
# module (SubprocessClient, InProcessClient) both exercise the JSONL envelope
# defined in SPEC §6, so a conforming server reached through them MUST
# advertise "jsonl" in its capabilities.transports list.
ACTIVE_TRANSPORT = "jsonl"


# --- read-only checks ----------------------------------------------------


def _ok(resp: dict) -> bool:
    """Return whether `resp` is a success envelope."""
    return bool(resp.get("ok"))


def _result(resp: dict) -> Any:
    """Return the `result` field of an envelope, or None."""
    return resp.get("result")


def _err(resp: dict) -> dict:
    """Return the `error` object of an envelope, or an empty dict."""
    return resp.get("error") or {}


def check_capabilities_shape(client: Client) -> CheckResult:
    """kb.capabilities returns the shape mandated by SPEC §9."""
    name = "capabilities.shape"
    resp = client.call("kb.capabilities")
    if not _ok(resp):
        return CheckResult(name, "capabilities", "fail",
                           f"kb.capabilities returned error: {_err(resp)}")
    caps = _result(resp)
    if not isinstance(caps, dict):
        return CheckResult(name, "capabilities", "fail",
                           f"expected object, got {type(caps).__name__}")
    required = ("version", "methods", "retrieval", "review_gated", "transports")
    missing = [k for k in required if k not in caps]
    if missing:
        return CheckResult(name, "capabilities", "fail",
                           f"missing required fields: {missing}")
    transports = caps.get("transports") or []
    if ACTIVE_TRANSPORT not in transports:
        return CheckResult(
            name, "capabilities", "fail",
            f"server reached over {ACTIVE_TRANSPORT!r} but capabilities.transports "
            f"is {transports!r} — must advertise the transport it speaks",
        )
    return CheckResult(name, "capabilities", "pass",
                       f"version={caps['version']} transports={caps['transports']}")


def check_capabilities_review_gated(client: Client) -> CheckResult:
    """SPEC §9: review_gated MUST be true for a conforming vouch server."""
    name = "capabilities.review_gated"
    resp = client.call("kb.capabilities")
    if not _ok(resp):
        return CheckResult(name, "capabilities", "skip",
                           "kb.capabilities did not return success")
    caps = _result(resp) or {}
    if caps.get("review_gated") is not True:
        return CheckResult(
            name, "capabilities", "fail",
            "SPEC §9: review_gated MUST be true for a conforming server, "
            f"got {caps.get('review_gated')!r}",
        )
    return CheckResult(name, "capabilities", "pass", "review_gated is true")


def check_capabilities_declares_spec_methods(client: Client) -> CheckResult:
    """All methods declared in SPEC §5 must appear in capabilities.methods."""
    name = "capabilities.declares_spec_methods"
    resp = client.call("kb.capabilities")
    if not _ok(resp):
        return CheckResult(name, "capabilities", "skip",
                           "kb.capabilities did not return success")
    declared = set((_result(resp) or {}).get("methods", []))
    missing = [m for m in SPEC_METHODS_ALL if m not in declared]
    if missing:
        return CheckResult(
            name, "capabilities", "fail",
            f"capabilities omits {len(missing)} SPEC-required methods: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}",
        )
    return CheckResult(name, "capabilities", "pass",
                       f"all {len(SPEC_METHODS_ALL)} SPEC methods declared")


def check_status_shape(client: Client) -> CheckResult:
    """kb.status must return a dict result on success."""
    name = "status.shape"
    resp = client.call("kb.status")
    if not _ok(resp):
        return CheckResult(name, "read", "fail",
                           f"kb.status returned error: {_err(resp)}")
    if not isinstance(_result(resp), dict):
        return CheckResult(name, "read", "fail",
                           f"expected object, got {type(_result(resp)).__name__}")
    return CheckResult(name, "read", "pass", "")


def _check_list_returns_list(method: str) -> Callable[[Client], CheckResult]:
    """Return a check that asserts `method` returns a JSON array on success."""

    def _do(client: Client) -> CheckResult:
        """Run the list-shape check for the bound `method`."""
        name = f"{method}.is_list"
        resp = client.call(method)
        if not _ok(resp):
            return CheckResult(name, "read", "fail",
                               f"{method} returned error: {_err(resp)}")
        if not isinstance(_result(resp), list):
            return CheckResult(
                name, "read", "fail",
                f"{method} must return an array, got {type(_result(resp)).__name__}",
            )
        return CheckResult(name, "read", "pass", "")
    # Expose a descriptive name so `run_suite`'s catch-all can identify the
    # check if `_do` raises — without this, the report would show "_do" for
    # every list check.
    _do.name = f"{method}.is_list"  # type: ignore[attr-defined]
    return _do


def check_search_shape(client: Client) -> CheckResult:
    """kb.search with a benign query must return an array (possibly empty)."""
    name = "search.shape"
    resp = client.call("kb.search", {"query": "vouch-conformance-probe", "limit": 1})
    if not _ok(resp):
        return CheckResult(name, "read", "fail",
                           f"kb.search returned error: {_err(resp)}")
    hits = _result(resp)
    if not isinstance(hits, list):
        return CheckResult(name, "read", "fail",
                           f"kb.search must return an array, got {type(hits).__name__}")
    for h in hits:
        if not isinstance(h, dict):
            return CheckResult(name, "read", "fail",
                               f"hit is not an object: {h!r}")
        missing = [k for k in ("kind", "id") if k not in h]
        if missing:
            return CheckResult(name, "read", "fail",
                               f"hit missing required keys {missing}: {h!r}")
    return CheckResult(name, "read", "pass", f"{len(hits)} hit(s)")


def check_unknown_method_returns_error(client: Client) -> CheckResult:
    """SPEC §6: unknown methods must surface as method_not_found."""
    name = "errors.unknown_method"
    resp = client.call("kb.this_method_does_not_exist", {})
    if _ok(resp):
        return CheckResult(name, "errors", "fail",
                           "server returned ok=true for an unknown method")
    code = _err(resp).get("code")
    if code != "method_not_found":
        return CheckResult(
            name, "errors", "fail",
            f"unknown method should yield code=method_not_found, got {code!r}",
        )
    return CheckResult(name, "errors", "pass", "")


def check_missing_param_returns_error(client: Client) -> CheckResult:
    """SPEC Section 6: a missing required param MUST surface as `missing_param`."""
    name = "errors.missing_param"
    resp = client.call("kb.read_claim", {})
    if _ok(resp):
        return CheckResult(name, "errors", "fail",
                           "kb.read_claim returned ok=true with no claim_id")
    code = _err(resp).get("code")
    if code != "missing_param":
        return CheckResult(
            name, "errors", "fail",
            f"missing required param should yield code='missing_param', got {code!r}",
        )
    return CheckResult(name, "errors", "pass", "")


READ_ONLY_CHECKS: tuple[Callable[[Client], CheckResult], ...] = (
    check_capabilities_shape,
    check_capabilities_review_gated,
    check_capabilities_declares_spec_methods,
    check_status_shape,
    _check_list_returns_list("kb.list_pages"),
    _check_list_returns_list("kb.list_claims"),
    _check_list_returns_list("kb.list_entities"),
    _check_list_returns_list("kb.list_relations"),
    _check_list_returns_list("kb.list_sources"),
    _check_list_returns_list("kb.list_pending"),
    check_search_shape,
    check_unknown_method_returns_error,
    check_missing_param_returns_error,
)


# --- mutating checks -----------------------------------------------------


def check_propose_claim_creates_pending(client: Client) -> CheckResult:
    """SPEC §4: kb.propose_claim writes to proposed/, surfaces in list_pending."""
    name = "review_gate.propose_creates_pending"
    src_resp = client.call("kb.register_source",
                           {"content": "conformance probe evidence",
                            "title": "conformance-probe",
                            "source_type": "file"})
    if not _ok(src_resp):
        return CheckResult(name, "write", "fail",
                           f"register_source failed: {_err(src_resp)}")
    src_id = (_result(src_resp) or {}).get("id")
    if not src_id:
        return CheckResult(name, "write", "fail",
                           "register_source result missing id")

    propose = client.call("kb.propose_claim",
                          {"text": "conformance probe claim",
                           "evidence": [src_id]})
    if not _ok(propose):
        return CheckResult(name, "write", "fail",
                           f"propose_claim failed: {_err(propose)}")
    pid = (_result(propose) or {}).get("proposal_id")
    if not pid:
        return CheckResult(name, "write", "fail",
                           "propose_claim result missing proposal_id")

    pending = client.call("kb.list_pending")
    if not _ok(pending):
        return CheckResult(name, "write", "fail",
                           f"list_pending failed: {_err(pending)}")
    ids = [p.get("id") for p in (_result(pending) or [])]
    if pid not in ids:
        return CheckResult(
            name, "write", "fail",
            f"proposal {pid} not in list_pending after propose_claim",
        )
    return CheckResult(name, "write", "pass", f"proposal {pid} pending")


def check_unapproved_claim_not_durable(client: Client) -> CheckResult:
    """A pending proposal must NOT yet appear in kb.list_claims."""
    name = "review_gate.proposal_not_durable_until_approved"
    src_resp = client.call("kb.register_source",
                           {"content": "gate probe evidence",
                            "title": "gate-probe",
                            "source_type": "file"})
    if not _ok(src_resp):
        return CheckResult(name, "write", "skip",
                           f"register_source failed: {_err(src_resp)}")
    src_id = (_result(src_resp) or {}).get("id")

    before = client.call("kb.list_claims")
    if not _ok(before):
        return CheckResult(name, "write", "fail",
                           f"list_claims (before) failed: {_err(before)}")
    before_ids = {c.get("id") for c in (_result(before) or [])}

    propose = client.call("kb.propose_claim",
                          {"text": "ungated probe", "evidence": [src_id]})
    if not _ok(propose):
        return CheckResult(name, "write", "skip",
                           f"propose_claim failed: {_err(propose)}")

    after = client.call("kb.list_claims")
    if not _ok(after):
        return CheckResult(name, "write", "fail",
                           f"list_claims (after) failed: {_err(after)}")
    after_ids = {c.get("id") for c in (_result(after) or [])}
    leaked = after_ids - before_ids
    if leaked:
        return CheckResult(
            name, "write", "fail",
            f"claim(s) appeared in list_claims without approval: {leaked}",
        )
    return CheckResult(name, "write", "pass",
                       "list_claims unchanged after propose_claim")


def check_approve_promotes_to_durable(client: Client) -> CheckResult:
    """kb.approve moves a proposal into the durable artifact directory."""
    name = "review_gate.approve_promotes"
    src_resp = client.call("kb.register_source",
                           {"content": "approve probe evidence",
                            "title": "approve-probe",
                            "source_type": "file"})
    if not _ok(src_resp):
        return CheckResult(name, "write", "skip",
                           f"register_source failed: {_err(src_resp)}")
    src_id = (_result(src_resp) or {}).get("id")

    propose = client.call("kb.propose_claim",
                          {"text": "approve probe claim",
                           "evidence": [src_id]})
    if not _ok(propose):
        return CheckResult(name, "write", "skip",
                           f"propose_claim failed: {_err(propose)}")
    pid = (_result(propose) or {}).get("proposal_id")

    approve = client.call("kb.approve", {"proposal_id": pid})
    if not _ok(approve):
        # SPEC §5.4: a server MAY require an approver != proposer. The
        # conformance suite sets VOUCH_AGENT=conformance-approver before
        # this call to avoid the self-approval gate; if the server still
        # rejects, treat that as the server's strict policy and skip.
        msg = _err(approve).get("message", "")
        if "forbidden_self_approval" in msg or "approver" in msg.lower():
            return CheckResult(name, "write", "skip",
                               f"server requires explicit approver: {msg}")
        return CheckResult(name, "write", "fail",
                           f"approve failed: {_err(approve)}")

    claims = client.call("kb.list_claims")
    if not _ok(claims):
        return CheckResult(name, "write", "fail",
                           f"list_claims failed: {_err(claims)}")
    claim_id = (_result(approve) or {}).get("id")
    if not any(c.get("id") == claim_id for c in (_result(claims) or [])):
        return CheckResult(
            name, "write", "fail",
            f"approved claim {claim_id} not in list_claims",
        )
    return CheckResult(name, "write", "pass", f"approved claim {claim_id}")


def check_reject_does_not_create_artifact(client: Client) -> CheckResult:
    """kb.reject closes a proposal without writing a durable claim."""
    name = "review_gate.reject_does_not_create"
    src_resp = client.call("kb.register_source",
                           {"content": "reject probe evidence",
                            "title": "reject-probe",
                            "source_type": "file"})
    if not _ok(src_resp):
        return CheckResult(name, "write", "skip",
                           f"register_source failed: {_err(src_resp)}")
    src_id = (_result(src_resp) or {}).get("id")

    before = client.call("kb.list_claims")
    if not _ok(before):
        return CheckResult(name, "write", "fail",
                           f"list_claims (before) failed: {_err(before)}")
    before_ids = {c.get("id") for c in (_result(before) or [])}

    propose = client.call("kb.propose_claim",
                          {"text": "reject probe claim",
                           "evidence": [src_id]})
    if not _ok(propose):
        return CheckResult(name, "write", "skip",
                           f"propose_claim failed: {_err(propose)}")
    pid = (_result(propose) or {}).get("proposal_id")

    reject = client.call("kb.reject", {"proposal_id": pid,
                                       "reason": "conformance probe"})
    if not _ok(reject):
        return CheckResult(name, "write", "fail",
                           f"reject failed: {_err(reject)}")

    after = client.call("kb.list_claims")
    if not _ok(after):
        return CheckResult(name, "write", "fail",
                           f"list_claims (after) failed: {_err(after)}")
    leaked = {c.get("id") for c in (_result(after) or [])} - before_ids
    if leaked:
        return CheckResult(
            name, "write", "fail",
            f"claim(s) appeared in list_claims after reject: {leaked}",
        )
    return CheckResult(name, "write", "pass", "rejected proposal left no artifact")


MUTATING_CHECKS: tuple[Callable[[Client], CheckResult], ...] = (
    check_propose_claim_creates_pending,
    check_unapproved_claim_not_durable,
    check_approve_promotes_to_durable,
    check_reject_does_not_create_artifact,
)


# --- runner --------------------------------------------------------------


def _check_label(check: Callable[[Client], CheckResult]) -> str:
    """Best-effort descriptive name for a check callable.

    Closures returned by factories like `_check_list_returns_list` set an
    explicit `name` attribute (e.g. `kb.list_claims.is_list`); top-level
    `check_*` functions fall back to `__name__`. Used by `run_suite`'s
    catch-all so a crashed check is identifiable in the report.
    """
    name: str | None = getattr(check, "name", None)
    return name or getattr(check, "__name__", "check") or "check"


def run_suite(client: Client, *, mutating: bool = False) -> ConformanceReport:
    """Run the conformance suite against `client` and return a report."""
    report = ConformanceReport()
    for check in READ_ONLY_CHECKS:
        try:
            report.results.append(check(client))
        except Exception as e:
            report.results.append(CheckResult(
                _check_label(check), "internal", "fail",
                f"check raised: {e!r}",
            ))
    if mutating:
        for check in MUTATING_CHECKS:
            try:
                report.results.append(check(client))
            except Exception as e:
                report.results.append(CheckResult(
                    _check_label(check), "internal", "fail",
                    f"check raised: {e!r}",
                ))
    return report


def format_report(report: ConformanceReport) -> str:
    """Render a report as plain text for the CLI."""
    lines = []
    icons = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    for r in report.results:
        line = f"  [{icons.get(r.status, '????')}] {r.name}"
        if r.message:
            line += f" — {r.message}"
        lines.append(line)
    c = report.counts
    lines.append("")
    lines.append(
        f"summary: {c.get('pass', 0)} pass / {c.get('fail', 0)} fail / "
        f"{c.get('skip', 0)} skip"
    )
    return "\n".join(lines)


def run_default(
    *,
    server_cmd: list[str] | None = None,
    target: Path | None = None,
    mutating: bool = False,
    env: dict[str, str] | None = None,
) -> ConformanceReport:
    """Spawn the default `vouch serve --transport jsonl` and run the suite.

    `target` is the working directory the server inherits; if it points at a
    `.vouch/` parent, the server uses that KB. Used by the CLI when no custom
    server command is supplied.
    """
    cmd = server_cmd or [sys.executable, "-m", "vouch.cli", "serve",
                         "--transport", "jsonl"]
    client = SubprocessClient(cmd, cwd=target, env=env)
    try:
        return run_suite(client, mutating=mutating)
    finally:
        client.close()
