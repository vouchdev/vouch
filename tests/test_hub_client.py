"""`vouch hub` client: link config, token store, push/pull/status."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from vouch import bundle, hub_client
from vouch.models import Claim
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path) -> KBStore:
    return KBStore.init(tmp_path / "kb")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.delenv("VOUCH_HUB_TOKEN", raising=False)
    return cfg


def make_bundle(files: dict[str, bytes]) -> tuple[bytes, str]:
    """Build a spec-conformant bundle in memory (mirror of the hub's builder)."""
    hashes = {p: hashlib.sha256(d).hexdigest() for p, d in files.items()}
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(hashes[p].encode())
    bundle_id = h.hexdigest()
    manifest = {
        "spec": "vouch-bundle-0.1",
        "bundle_id": bundle_id,
        "files": [
            {"path": p, "size": len(d), "sha256": hashes[p]}
            for p, d in sorted(files.items())
        ],
        "counts": {},
        "excluded": ["config.yaml", "decided", "sessions"],
        "safety": {"has_proposed": False, "has_state_db": False, "has_audit_log": False},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p, d in sorted(files.items()):
            info = tarfile.TarInfo(p)
            info.size = len(d)
            tar.addfile(info, io.BytesIO(d))
        mbytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(mbytes)
        tar.addfile(info, io.BytesIO(mbytes))
    return buf.getvalue(), bundle_id


def exported_files(kb_dir: Path, tmp_path: Path) -> dict[str, bytes]:
    """The KB's knowledge-only export, as {path: bytes}."""
    dest = tmp_path / "exp.tar.gz"
    bundle.export(kb_dir, dest=dest, exclude=hub_client.SYNC_EXCLUDE)
    out: dict[str, bytes] = {}
    with tarfile.open(dest, "r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile() and m.name != "manifest.json":
                out[m.name] = tar.extractfile(m).read()  # type: ignore[union-attr]
    return out


class FakeHub(BaseHTTPRequestHandler):
    """Scripted hub speaking the v2 wire contract for one KB."""

    files: ClassVar[dict[str, bytes]] = {}
    token = "vhp_test"
    conflicts_on_push: ClassVar[list[str]] = []

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.token}"

    def do_GET(self) -> None:  # BaseHTTPRequestHandler API name
        if not self._authed():
            self.send_response(401)
            self.end_headers()
            return
        gz, bundle_id = make_bundle(self.files)
        etag = f'"{bundle_id}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Type", "application/gzip")
        self.end_headers()
        self.wfile.write(gz)

    def do_PUT(self) -> None:  # BaseHTTPRequestHandler API name
        if not self._authed():
            self.send_response(401)
            self.end_headers()
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.conflicts_on_push:
            self._json(
                409,
                {
                    "error": "conflicting artifacts",
                    "conflicts": self.conflicts_on_push,
                    "new_files": [],
                },
            )
            return
        self._json(200, {"ok": True, "bundle_id": "b" * 64, "written": 3, "identical": 0})

    def _json(self, status: int, obj: object) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:  # silence
        del args


@pytest.fixture
def fake_hub():
    FakeHub.files = {}
    FakeHub.conflicts_on_push = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _link(store: KBStore, url: str) -> hub_client.HubLink:
    link = hub_client.HubLink(url=url, kb="alice/proj", last_bundle_id=None)
    hub_client.save_link(store.kb_dir, link)
    return link


# --- config + tokens ---------------------------------------------------------


def test_link_round_trip(store: KBStore) -> None:
    assert hub_client.load_link(store.kb_dir) is None
    _link(store, "http://h")
    loaded = hub_client.load_link(store.kb_dir)
    assert loaded is not None and loaded.kb == "alice/proj" and loaded.url == "http://h"


def test_link_file_is_never_exported(store: KBStore, tmp_path: Path) -> None:
    _link(store, "http://h")
    assert "hub.yaml" not in exported_files(store.kb_dir, tmp_path)


def test_token_env_beats_file_and_file_is_0600(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_client.save_token("http://h", "vhp_file")
    cred = home / "vouch" / "hub.yaml"
    assert cred.exists()
    assert (cred.stat().st_mode & 0o777) == 0o600
    assert hub_client.resolve_token("http://h") == "vhp_file"
    assert hub_client.resolve_token("http://other") is None
    monkeypatch.setenv("VOUCH_HUB_TOKEN", "vhp_env")
    assert hub_client.resolve_token("http://h") == "vhp_env"


# --- push ---------------------------------------------------------------------


def test_push_happy_path(store: KBStore, fake_hub: str, home: Path) -> None:
    link = _link(store, fake_hub)
    r = hub_client.push(store, link, "vhp_test")
    assert r["status"] == "pushed"
    assert r["written"] == 3
    reloaded = hub_client.load_link(store.kb_dir)
    assert reloaded is not None and reloaded.last_bundle_id


def test_push_conflict_maps_to_HubConflict(store: KBStore, fake_hub: str, home: Path) -> None:
    FakeHub.conflicts_on_push = ["claims/c1.yaml"]
    link = _link(store, fake_hub)
    with pytest.raises(hub_client.HubConflict) as e:
        hub_client.push(store, link, "vhp_test")
    assert e.value.conflicts == ["claims/c1.yaml"]


def test_push_bad_token_raises_HubError(store: KBStore, fake_hub: str, home: Path) -> None:
    link = _link(store, fake_hub)
    with pytest.raises(hub_client.HubError):
        hub_client.push(store, link, "vhp_wrong")


# --- pull ---------------------------------------------------------------------


def _remote_knowledge(tmp_path: Path) -> dict[str, bytes]:
    """A real knowledge bundle (a cited claim + its source) as {path: bytes}.

    Unlike a bare `claims/r1.yaml` with no evidence, this is what a genuine hub
    push ships -- and the gated pull enforces vouch's citation rule, so the
    claim must carry a source it can quote.
    """
    src = KBStore.init(tmp_path / "remote-kb")
    source = src.put_source(b"advisory locks are session scoped", title="doc")
    src.put_claim(Claim(id="r1", text="advisory locks are session scoped", evidence=[source.id]))
    return exported_files(src.kb_dir, tmp_path / "remote-exp")


def test_pull_files_proposals_not_committed_writes(
    store: KBStore, fake_hub: str, home: Path, tmp_path: Path
) -> None:
    from vouch.models import ProposalKind, ProposalStatus
    from vouch.storage import ArtifactNotFoundError

    FakeHub.files = _remote_knowledge(tmp_path)
    link = _link(store, fake_hub)
    r = hub_client.pull(store, link, "vhp_test")

    # The gate held: inbound knowledge is a pending PROPOSAL, not a committed claim.
    assert r["status"] == "proposed"
    assert r["proposed"] == 1
    assert r["origin_kb"] == "alice/proj"  # provenance = the linked KB
    with pytest.raises(ArtifactNotFoundError):
        store.get_claim("r1")
    pending = [
        p for p in store.list_proposals(ProposalStatus.PENDING) if p.kind == ProposalKind.CLAIM
    ]
    assert len(pending) == 1
    assert "alice/proj" in pending[0].proposed_by

    reloaded = hub_client.load_link(store.kb_dir)
    assert reloaded is not None
    r2 = hub_client.pull(store, reloaded, "vhp_test")
    assert r2["status"] == "up_to_date"


def test_pull_files_a_proposal_even_when_a_local_claim_exists(
    store: KBStore, fake_hub: str, home: Path, tmp_path: Path
) -> None:
    from vouch.models import ProposalStatus

    FakeHub.files = _remote_knowledge(tmp_path)
    src = store.put_source(b"local evidence")
    store.put_claim(Claim(id="r1", text="local version", evidence=[src.id]))
    link = _link(store, fake_hub)

    r = hub_client.pull(store, link, "vhp_test")
    # No destructive overwrite: the local claim is untouched, the inbound one is
    # just another proposal the reviewer will weigh.
    assert r["status"] == "proposed"
    assert store.get_claim("r1").text == "local version"
    assert store.list_proposals(ProposalStatus.PENDING)


# --- cli ------------------------------------------------------------------------


def test_cli_link_push_status_pull(
    store: KBStore, fake_hub: str, home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(store.kb_dir.parent)
    runner = CliRunner()

    r = runner.invoke(cli, ["hub", "link", "alice/proj", "--url", fake_hub, "--token", "vhp_test"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["linked"] is True

    r = runner.invoke(cli, ["hub", "push"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["status"] in {"pushed", "up_to_date"}

    r = runner.invoke(cli, ["hub", "status"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["linked"] is True

    FakeHub.files = _remote_knowledge(tmp_path)
    r = runner.invoke(cli, ["hub", "pull"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["status"] == "proposed"


def test_cli_unlinked_errors_clearly(
    store: KBStore, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    monkeypatch.chdir(store.kb_dir.parent)
    r = CliRunner().invoke(cli, ["hub", "push"])
    assert r.exit_code != 0
    assert "vouch hub link" in r.output


# --- status ---------------------------------------------------------------------


def test_status_reports_sync_state(
    store: KBStore, fake_hub: str, home: Path, tmp_path: Path
) -> None:
    src = store.put_source(b"e")
    store.put_claim(Claim(id="c1", text="local knowledge", evidence=[src.id]))
    link = _link(store, fake_hub)
    s = hub_client.status(store, link, "vhp_test")
    assert s["linked"] is True
    assert s["in_sync"] is False  # remote empty, local has knowledge
    FakeHub.files = exported_files(store.kb_dir, tmp_path)
    s2 = hub_client.status(store, link, "vhp_test")
    assert s2["in_sync"] is True
