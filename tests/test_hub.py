"""KB identity, the machine registry, and hijack-proof resolution (hub substrate)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from vouch import audit, bundle, health, hub
from vouch.models import Claim
from vouch.storage import KBNotFoundError, KBStore, discover_root


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path_factory, monkeypatch):
    """Fake $HOME + registry path so tests never touch the real machine.

    Also clears the resolution env overrides — the developer's shell (or a
    hook harness) may carry VOUCH_KB_PATH / VOUCH_PROJECT_DIR, which would
    change what discover_root resolves.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv(hub.REGISTRY_ENV, str(fake_home / "registry.yaml"))
    monkeypatch.delenv("VOUCH_KB_PATH", raising=False)
    monkeypatch.delenv("VOUCH_PROJECT_DIR", raising=False)
    return fake_home


# --- kb identity -----------------------------------------------------------


def test_init_mints_identity(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "proj")
    identity = store.identity()
    assert identity is not None
    kb_id, name = identity
    assert len(kb_id) == 32
    assert name == "proj"
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    assert cfg["kb"]["id"] == kb_id
    # the starter config must still be intact next to the identity
    assert cfg["review"]["auto_approve_on_receipt"] is True


def test_concurrent_identity_minting_yields_one_id(tmp_path: Path) -> None:
    """Two processes racing ensure_identity on a legacy KB must converge on
    ONE id (flock-serialized read-mint-write) — a split mint would strand
    every artifact stamped with the losing id."""
    import concurrent.futures
    import subprocess
    import sys

    store = KBStore.init(tmp_path)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    del cfg["kb"]
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    code = (
        "from pathlib import Path; from vouch.storage import KBStore; "
        f"print(KBStore(Path({str(tmp_path)!r})).ensure_identity()[0])"
    )

    def _mint() -> str:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(lambda _i: _mint(), range(6)))
    assert len(set(ids)) == 1, ids
    assert store.identity() is not None
    assert store.identity()[0] == ids[0]  # type: ignore[index]


def test_ensure_identity_is_idempotent(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    first = store.ensure_identity()
    assert store.ensure_identity() == first
    assert KBStore.init(tmp_path).identity() == first  # re-init keeps it


def test_init_backfills_identity_for_legacy_kb(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    del cfg["kb"]  # simulate a pre-identity KB
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert store.identity() is None
    KBStore.init(tmp_path)
    assert store.identity() is not None


def test_ensure_identity_refuses_corrupt_config_untouched(tmp_path: Path) -> None:
    """Minting must never be the operation that destroys user settings."""
    store = KBStore.init(tmp_path)
    corrupt = "review:\n  broken: [unclosed\n"
    store.config_path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(ValueError):
        store.ensure_identity()
    assert store.config_path.read_text(encoding="utf-8") == corrupt
    non_mapping = "- just\n- a\n- list\n"
    store.config_path.write_text(non_mapping, encoding="utf-8")
    with pytest.raises(ValueError):
        store.ensure_identity()
    assert store.config_path.read_text(encoding="utf-8") == non_mapping


def test_identity_backfill_preserves_comments(tmp_path: Path) -> None:
    """The common backfill appends `kb:` textually — comments survive."""
    store = KBStore.init(tmp_path)
    legacy = "# tuned for our team\nreview:\n  auto_approve_on_receipt: true\n"
    store.config_path.write_text(legacy, encoding="utf-8")
    kb_id, _name = store.ensure_identity()
    text = store.config_path.read_text(encoding="utf-8")
    assert text.startswith(legacy)  # original bytes untouched
    assert f"id: {kb_id}" in text
    cfg = yaml.safe_load(text)
    assert cfg["review"]["auto_approve_on_receipt"] is True
    assert cfg["kb"]["id"] == kb_id


def test_reinit_backfill_logs_kb_identity_event(tmp_path: Path) -> None:
    """Re-running `vouch init` on a pre-identity KB records the mint."""
    from click.testing import CliRunner

    from vouch.cli import cli

    runner = CliRunner()
    proj = tmp_path / "proj"
    assert runner.invoke(cli, ["init", "--path", str(proj)]).exit_code == 0
    store = KBStore(proj)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    del cfg["kb"]
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert runner.invoke(cli, ["init", "--path", str(proj)]).exit_code == 0
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert events.count("kb.identity") == 1
    # a fresh init must NOT emit it (identity minted at birth, not backfilled)
    fresh = tmp_path / "fresh"
    assert runner.invoke(cli, ["init", "--path", str(fresh)]).exit_code == 0
    fresh_events = [e.event for e in audit.read_events(KBStore(fresh).kb_dir)]
    assert "kb.identity" not in fresh_events


def test_ensure_identity_refuses_without_config(tmp_path: Path) -> None:
    # Creating a bare config here would make a later init() skip the
    # starter config, so an un-inited store must refuse instead.
    with pytest.raises(KBNotFoundError):
        KBStore(tmp_path).ensure_identity()
    assert not (tmp_path / ".vouch" / "config.yaml").exists()


def test_audit_events_carry_kb_id(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    kb_id = store.identity()[0]  # type: ignore[index]
    ev = audit.log_event(store.kb_dir, event="test.event", actor="t")
    assert ev.kb_id == kb_id
    assert audit.verify_chain(store.kb_dir).ok


def test_chain_verifies_across_identity_backfill(tmp_path: Path) -> None:
    """Legacy (kb_id-less) events followed by stamped events stay one chain."""
    store = KBStore.init(tmp_path)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    kb_block = cfg.pop("kb")
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    legacy = audit.log_event(store.kb_dir, event="legacy.event", actor="t")
    assert legacy.kb_id is None
    cfg["kb"] = kb_block
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    stamped = audit.log_event(store.kb_dir, event="stamped.event", actor="t")
    assert stamped.kb_id == kb_block["id"]
    assert audit.verify_chain(store.kb_dir).ok


def test_bundle_manifest_carries_kb_identity(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    src = store.put_source(b"evidence")
    store.put_claim(Claim(id="c1", text="a fact", evidence=[src.id]))
    manifest = bundle.build_manifest(store.kb_dir)
    identity = store.identity()
    assert identity is not None
    assert manifest["kb"] == {"id": identity[0], "name": identity[1]}


def test_legacy_audit_line_without_kb_id_key_still_verifies(tmp_path: Path) -> None:
    """A genuine pre-upgrade log line (kb_id key ABSENT, not null) chains on."""
    store = KBStore.init(tmp_path)
    payload = {
        "id": audit.new_event_id(),
        "event": "legacy.event",
        "actor": "t",
        "created_at": "2026-01-01T00:00:00+00:00",
        "object_ids": [],
        "dry_run": False,
        "reversible": True,
        "data": {},
        "prev_hash": audit.GENESIS_HASH,
    }
    payload["hash"] = audit._compute_hash(
        audit.GENESIS_HASH, {k: v for k, v in payload.items() if k != "hash"}
    )
    (store.kb_dir / audit.AUDIT_FILENAME).write_text(
        audit._canonical_json(payload) + "\n", encoding="utf-8"
    )
    assert audit.verify_chain(store.kb_dir).ok
    audit.log_event(store.kb_dir, event="new.event", actor="t")
    assert audit.verify_chain(store.kb_dir).ok


def test_bundle_import_never_adopts_source_identity(tmp_path: Path) -> None:
    """Settings move in bundles; identity never does — even on overwrite."""
    src_store = KBStore.init(tmp_path / "src")
    source = src_store.put_source(b"evidence")
    src_store.put_claim(Claim(id="c1", text="a fact", evidence=[source.id]))
    bundle_path = tmp_path / "kb.tar.gz"
    bundle.export(src_store.kb_dir, dest=bundle_path)

    dest_store = KBStore.init(tmp_path / "dst")
    dest_identity = dest_store.identity()
    check = bundle.import_check(dest_store.kb_dir, bundle_path)
    # same starter settings, different ids -> identical, not a conflict
    assert "config.yaml" not in check.conflicts
    bundle.import_apply(dest_store.kb_dir, bundle_path, on_conflict="overwrite")
    assert dest_store.identity() == dest_identity


def test_legacy_commented_bundle_converges(tmp_path: Path) -> None:
    """A pre-identity, comment-bearing bundle with the same settings reads
    identical (structural compare), and skip-mode import leaves the
    destination's own config bytes untouched."""
    src_store = KBStore.init(tmp_path / "src")
    cfg = yaml.safe_load(src_store.config_path.read_text(encoding="utf-8"))
    cfg.pop("kb")
    src_store.config_path.write_text(
        "# exported by old vouch\n" + yaml.safe_dump(cfg, sort_keys=False),
        encoding="utf-8",
    )
    source = src_store.put_source(b"evidence")
    src_store.put_claim(Claim(id="c1", text="a fact", evidence=[source.id]))
    bundle_path = tmp_path / "kb.tar.gz"
    bundle.export(src_store.kb_dir, dest=bundle_path)

    dest_store = KBStore.init(tmp_path / "dst")
    dest_bytes = dest_store.config_path.read_bytes()
    check = bundle.import_check(dest_store.kb_dir, bundle_path)
    assert "config.yaml" in check.identical
    result = bundle.import_apply(dest_store.kb_dir, bundle_path, on_conflict="skip")
    assert "config.yaml" not in result["written"]
    assert dest_store.config_path.read_bytes() == dest_bytes  # untouched
    # and a re-check after import still converges — no permanent conflict
    recheck = bundle.import_check(dest_store.kb_dir, bundle_path)
    assert "config.yaml" in recheck.identical


def test_import_skips_corrupt_dest_config(tmp_path: Path) -> None:
    """An unparseable destination config (latent identity) is never overwritten."""
    src_store = KBStore.init(tmp_path / "src")
    source = src_store.put_source(b"evidence")
    src_store.put_claim(Claim(id="c1", text="a fact", evidence=[source.id]))
    bundle_path = tmp_path / "kb.tar.gz"
    bundle.export(src_store.kb_dir, dest=bundle_path)

    dest_store = KBStore.init(tmp_path / "dst")
    corrupt = b"review:\n  broken: [unclosed\n"
    dest_store.config_path.write_bytes(corrupt)
    result = bundle.import_apply(
        dest_store.kb_dir, bundle_path, on_conflict="overwrite"
    )
    assert "config.yaml" in result["skipped_conflicts"]
    assert dest_store.config_path.read_bytes() == corrupt


def test_status_reports_identity(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path)
    s = health.status(store)
    identity = store.identity()
    assert identity is not None
    assert s["kb_id"] == identity[0]
    assert s["kb_name"] == identity[1]


# --- resolution: env precedence + $HOME walk-stop ---------------------------


def test_vouch_project_dir_overrides_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    KBStore.init(proj)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(KBNotFoundError):
        discover_root()
    monkeypatch.setenv("VOUCH_PROJECT_DIR", str(proj))
    assert discover_root() == proj


def test_explicit_start_beats_project_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    KBStore.init(proj_a)
    KBStore.init(proj_b)
    monkeypatch.setenv("VOUCH_PROJECT_DIR", str(proj_a))
    assert discover_root(proj_b) == proj_b


def test_project_dir_env_ignored_when_not_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    KBStore.init(proj)
    monkeypatch.setenv("VOUCH_PROJECT_DIR", str(tmp_path / "gone"))
    monkeypatch.chdir(proj)
    assert discover_root() == proj


def test_home_kb_never_captures_projects_below(_isolated_machine: Path) -> None:
    """The recorded ~/.vouch hijack: a home KB must not win by ancestry."""
    home = _isolated_machine
    KBStore.init(home)
    proj = home / "Dev" / "myproj"
    proj.mkdir(parents=True)
    with pytest.raises(KBNotFoundError) as exc:
        discover_root(proj)
    assert "home-directory KB" in str(exc.value)


def test_walk_never_ascends_past_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with no home KB, resolution stops at $HOME instead of finding
    a stray .vouch above it (e.g. /tmp-root fixtures or /.vouch)."""
    outer = tmp_path / "outer"
    home = outer / "home"
    KBStore.init(outer)  # a .vouch ABOVE this test's private home
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    proj = home / "Dev" / "myproj"
    proj.mkdir(parents=True)
    with pytest.raises(KBNotFoundError) as exc:
        discover_root(proj)
    assert "stopped at" in str(exc.value)


def test_home_kb_resolves_when_started_in_home(_isolated_machine: Path) -> None:
    home = _isolated_machine
    KBStore.init(home)
    assert discover_root(home) == home


def test_home_kb_allowed_by_config_optin(_isolated_machine: Path) -> None:
    home = _isolated_machine
    store = KBStore.init(home)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    cfg["global"] = {"allow_home_kb": True}
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    proj = home / "Dev" / "myproj"
    proj.mkdir(parents=True)
    assert discover_root(proj) == home


def test_vouch_kb_path_still_reaches_home_kb(
    _isolated_machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit override stays the deliberate escape hatch."""
    home = _isolated_machine
    KBStore.init(home)
    monkeypatch.setenv("VOUCH_KB_PATH", str(home / ".vouch"))
    proj = home / "Dev" / "myproj"
    proj.mkdir(parents=True)
    assert discover_root(proj) == home


def test_project_kb_still_resolves_under_home(_isolated_machine: Path) -> None:
    """The walk-stop must not break the normal case: project under $HOME."""
    home = _isolated_machine
    KBStore.init(home)  # hostile ancestor present
    proj = home / "Dev" / "myproj"
    KBStore.init(proj)
    nested = proj / "src" / "deep"
    nested.mkdir(parents=True)
    assert discover_root(nested) == proj


def test_homeless_container_falls_back_to_plain_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path.home() raising (stripped env) must not break resolution."""

    def _boom(cls: type) -> Path:
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(_boom))
    proj = tmp_path / "proj"
    KBStore.init(proj)
    nested = proj / "deep"
    nested.mkdir()
    assert discover_root(nested) == proj
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    with pytest.raises(KBNotFoundError):
        discover_root(lonely)


def test_discover_trace_names_the_rule_that_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    KBStore.init(proj)
    trace: list[str] = []
    discover_root(proj, trace=trace)
    assert any("found .vouch/ at" in line for line in trace)
    monkeypatch.setenv("VOUCH_PROJECT_DIR", str(proj))
    trace = []
    discover_root(trace=trace)
    assert any("VOUCH_PROJECT_DIR" in line for line in trace)


# --- registry ---------------------------------------------------------------


def test_register_list_unregister_roundtrip(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    store = KBStore.init(proj)
    entry = hub.register_kb(proj, actor="t")
    assert entry.kb_id == store.identity()[0]  # type: ignore[index]
    assert entry.role == "project"
    listed = hub.load_registry()
    assert [e.kb_id for e in listed] == [entry.kb_id]
    assert hub.unregister_kb(entry.kb_id) is not None
    assert hub.load_registry() == []


def test_register_is_idempotent_and_keeps_added_at(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    KBStore.init(proj)
    first = hub.register_kb(proj, actor="t")
    again = hub.register_kb(proj, role="personal", actor="t")
    assert len(hub.load_registry()) == 1
    assert again.added_at == first.added_at
    assert again.role == "personal"


def test_register_backfills_identity_with_audit_event(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    store = KBStore.init(proj)
    cfg = yaml.safe_load(store.config_path.read_text(encoding="utf-8"))
    del cfg["kb"]
    store.config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    hub.register_kb(proj, actor="t")
    assert store.identity() is not None
    events = [e.event for e in audit.read_events(store.kb_dir)]
    assert "kb.identity" in events


def test_unregister_by_path_and_tilde(
    _isolated_machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _isolated_machine
    monkeypatch.setenv("HOME", str(home))  # expanduser reads $HOME, not Path.home
    proj = home / "proj"
    KBStore.init(proj)
    hub.register_kb(proj, actor="t")
    assert hub.unregister_kb(str(proj)) is not None  # absolute path token
    hub.register_kb(proj, actor="t")
    assert hub.unregister_kb("~/proj") is not None  # tilde token
    assert hub.load_registry() == []


def test_register_refuses_non_kb_path(tmp_path: Path) -> None:
    with pytest.raises(KBNotFoundError):
        hub.register_kb(tmp_path / "nowhere", actor="t")


def test_corrupt_registry_degrades_to_empty(tmp_path: Path) -> None:
    hub.registry_path().parent.mkdir(parents=True, exist_ok=True)
    hub.registry_path().write_text(":\nnot yaml [", encoding="utf-8")
    assert hub.load_registry() == []


# --- role guard -------------------------------------------------------------


def _personal_ancestor_setup(tmp_path: Path) -> tuple[Path, Path]:
    """A personal-role KB whose directory contains a plain project dir."""
    parent = tmp_path / "notes"
    KBStore.init(parent)
    hub.register_kb(parent, role="personal", actor="t")
    child = parent / "someproject"
    child.mkdir()
    return parent, child


def test_capture_refuses_personal_role_ancestor(tmp_path: Path) -> None:
    parent, child = _personal_ancestor_setup(tmp_path)
    res = hub.resolve(child)
    assert res.root == parent
    assert res.guard is not None
    assert hub.resolve_for_capture(child) is None


def test_reads_survive_guard_with_warning(tmp_path: Path) -> None:
    """Recall must never go dark under the guard — warn and proceed."""
    parent, child = _personal_ancestor_setup(tmp_path)
    store, warning = hub.resolve_for_read(child)
    assert store is not None
    assert store.root == parent
    assert warning is not None  # surfaced on stderr by the CLI callers


def test_capture_allowed_at_personal_kb_root(tmp_path: Path) -> None:
    parent, _child = _personal_ancestor_setup(tmp_path)
    res = hub.resolve(parent)
    assert res.guard is None
    assert hub.resolve_for_capture(parent) is not None


def test_project_role_ancestor_is_not_guarded(tmp_path: Path) -> None:
    parent = tmp_path / "mono"
    KBStore.init(parent)
    hub.register_kb(parent, role="project", actor="t")
    child = parent / "pkg"
    child.mkdir()
    res = hub.resolve(child)
    assert res.root == parent
    assert res.guard is None


def test_vouch_kb_path_disables_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = _personal_ancestor_setup(tmp_path)
    monkeypatch.setenv("VOUCH_KB_PATH", str(parent / ".vouch"))
    res = hub.resolve(child)
    assert res.root == parent
    assert res.guard is None


def test_resolve_reports_why_on_missing_kb(_isolated_machine: Path) -> None:
    lonely = _isolated_machine / "Dev" / "lonely"
    lonely.mkdir(parents=True)
    res = hub.resolve(lonely)
    assert res.root is None
    assert res.why  # the chain explains the failure instead of raising


# --- cli --------------------------------------------------------------------


def test_cli_hub_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from vouch.cli import cli

    proj = tmp_path / "proj"
    KBStore.init(proj)
    runner = CliRunner()
    r = runner.invoke(cli, ["hub", "register", "--path", str(proj)])
    assert r.exit_code == 0, r.output
    assert "registered" in r.output
    r = runner.invoke(cli, ["hub", "list", "--json"])
    assert r.exit_code == 0, r.output
    listed = json.loads(r.output)
    assert len(listed["kbs"]) == 1
    kb_id = listed["kbs"][0]["kb_id"]
    r = runner.invoke(cli, ["hub", "unregister", kb_id])
    assert r.exit_code == 0, r.output
    r = runner.invoke(cli, ["hub", "unregister", kb_id])
    assert r.exit_code != 0  # already gone -> clean error, not a traceback


def test_cli_capture_store_respects_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vouch.cli import _capture_store

    _parent, child = _personal_ancestor_setup(tmp_path)
    monkeypatch.chdir(child)
    assert _capture_store() is None
