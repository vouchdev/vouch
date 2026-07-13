"""kb.list_skills / kb.get_skill — Claude Code skill discovery over MCP,
plus the ``mcp.publish_skills`` gate (issue #235)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import capabilities as caps_mod
from vouch import skills as skills_mod
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    # Point HOME at a clean dir so the test never picks up the real user's
    # ~/.claude/ skills and isn't polluted by it.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    s = KBStore.init(tmp_path / "kb")
    return s


def _write_skill(base: Path, name: str, *, body: str, frontmatter: bool = True) -> Path:
    skill_dir = base / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    text = body if not frontmatter else (
        f"---\nname: {name}\ndescription: this is the {name} skill\n---\n\n{body}"
    )
    path.write_text(text, encoding="utf-8")
    return path


def _write_command(base: Path, name: str, *, body: str) -> Path:
    cmd_dir = base / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    path = cmd_dir / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _set_publish_skills(store: KBStore, value: object) -> None:
    """Rewrite config.yaml's mcp.publish_skills flag (or remove the block)."""
    cfg = yaml.safe_load(store.config_path.read_text()) or {}
    if value is None:
        cfg.pop("mcp", None)
    else:
        cfg.setdefault("mcp", {})["publish_skills"] = value
    store.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))


def test_list_skills_scans_project_local(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "vouch-recall", body="# vouch-recall\n\nCall kb.recall.")
    rows = skills_mod.list_skills(store)
    names = [r["name"] for r in rows]
    assert "vouch-recall" in names
    rec = next(r for r in rows if r["name"] == "vouch-recall")
    assert rec["scope"] == "project"
    assert rec["kind"] == "skill"
    assert rec["description"] == "this is the vouch-recall skill"


def test_list_skills_scans_user_global(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path(skills_mod.Path.home())
    _write_skill(home / ".claude", "global-skill", body="# global-skill\n\nbody")
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "global-skill")
    assert rec["scope"] == "user"


def test_project_overrides_user_on_collision(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path(skills_mod.Path.home())
    _write_skill(home / ".claude", "shared-name", body="# user version")
    _write_skill(store.root / ".claude", "shared-name", body="# project version")
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "shared-name")
    assert rec["scope"] == "project"


def test_list_includes_slash_commands(store: KBStore) -> None:
    _write_command(
        store.root / ".claude", "vouch-status",
        body="# vouch-status\n\nShow status.\n\nMore details follow.",
    )
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "vouch-status")
    assert rec["kind"] == "command"
    # No frontmatter — description derived from first body paragraph after heading.
    assert "Show status" in rec["description"]


def test_get_skill_returns_full_body(store: KBStore) -> None:
    body_text = (
        "---\nname: vouch-recall\ndescription: the recall skill\n---\n\n"
        "# vouch-recall\n\nFull body of the skill.\n"
    )
    skill_dir = store.root / ".claude" / "skills" / "vouch-recall"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body_text, encoding="utf-8")

    result = skills_mod.get_skill(store, "vouch-recall")
    assert result["name"] == "vouch-recall"
    assert result["scope"] == "project"
    assert "Full body of the skill" in result["body"]


def test_get_skill_unknown_raises_keyerror(store: KBStore) -> None:
    with pytest.raises(KeyError):
        skills_mod.get_skill(store, "no-such-skill")


def test_list_skills_empty_environment(store: KBStore) -> None:
    """No .claude/ dirs in either project or fake HOME — returns []."""
    assert skills_mod.list_skills(store) == []


def test_unreadable_skill_does_not_break_listing(store: KBStore) -> None:
    """Best-effort discovery — a broken file is skipped silently."""
    skill_dir = store.root / ".claude" / "skills" / "good"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: good\n---\n\nbody", encoding="utf-8")
    # A directory with no SKILL.md should be skipped, not raise.
    (store.root / ".claude" / "skills" / "no-skill-md").mkdir()

    rows = skills_mod.list_skills(store)
    names = [r["name"] for r in rows]
    assert "good" in names
    assert "no-skill-md" not in names


def test_description_from_frontmatter_takes_priority(store: KBStore) -> None:
    body = (
        "---\nname: hi\ndescription: from frontmatter\n---\n\n# hi\n\nFrom body."
    )
    skill_dir = store.root / ".claude" / "skills" / "hi"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "hi")
    assert rec["description"] == "from frontmatter"


def test_description_falls_back_to_first_paragraph(store: KBStore) -> None:
    body = "# header line\n\nThis is the first paragraph.\n\nSecond paragraph."
    skill_dir = store.root / ".claude" / "skills" / "no-fm"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "no-fm")
    assert rec["description"] == "This is the first paragraph."


# --- publish_skills gate (issue #235) -------------------------------------


def test_starter_config_defaults_publish_skills_on(store: KBStore) -> None:
    """A freshly init'd KB ships mcp.publish_skills: true."""
    cfg = yaml.safe_load(store.config_path.read_text())
    assert cfg["mcp"]["publish_skills"] is True
    assert skills_mod.publish_skills_enabled(store) is True


def test_missing_mcp_block_defaults_on(store: KBStore) -> None:
    """An existing KB with no mcp: block sees the catalogue (default-on)."""
    _set_publish_skills(store, None)
    assert skills_mod.publish_skills_enabled(store) is True
    _write_skill(store.root / ".claude", "visible", body="# visible")
    assert any(r["name"] == "visible" for r in skills_mod.list_skills(store))


def test_list_skills_hidden_when_disabled(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "secret", body="# secret")
    # Visible by default...
    assert any(r["name"] == "secret" for r in skills_mod.list_skills(store))
    # ...hidden once the gate is flipped, with no restart.
    _set_publish_skills(store, False)
    assert skills_mod.publish_skills_enabled(store) is False
    assert skills_mod.list_skills(store) == []


def test_get_skill_denied_when_disabled(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "secret", body="# secret\n\nbody")
    _set_publish_skills(store, False)
    with pytest.raises(skills_mod.SkillsDisabledError):
        skills_mod.get_skill(store, "secret")


def test_toggle_takes_effect_without_restart(store: KBStore) -> None:
    """The flag is read fresh on each call — same store object, no reload."""
    _write_skill(store.root / ".claude", "toggle-me", body="# toggle-me")
    assert skills_mod.list_skills(store)  # on
    _set_publish_skills(store, False)
    assert skills_mod.list_skills(store) == []  # off
    _set_publish_skills(store, True)
    assert skills_mod.list_skills(store)  # on again


def test_capabilities_surfaces_publish_skills_flag() -> None:
    caps_on = caps_mod.capabilities(publish_skills=True)
    assert caps_on.mcp["publish_skills"] is True
    caps_off = caps_mod.capabilities(publish_skills=False)
    assert caps_off.mcp["publish_skills"] is False
    # New methods are declared in the surface.
    assert "kb.list_skills" in caps_on.methods
    assert "kb.get_skill" in caps_on.methods
    # Default is on so test_capabilities (no-arg call) stays green.
    assert caps_mod.capabilities().mcp["publish_skills"] is True


# --- jsonl wiring ---------------------------------------------------------


def test_jsonl_list_skills_handler(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(store.root / ".claude", "wired", body="# wired")
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r1", "method": "kb.list_skills", "params": {}},
    )
    assert out["ok"] is True
    assert any(r["name"] == "wired" for r in out["result"])


def test_jsonl_get_skill_handler(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(store.root / ".claude", "wired", body="# wired\n\nbody here")
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r2", "method": "kb.get_skill", "params": {"name": "wired"}},
    )
    assert out["ok"] is True
    assert "body here" in out["result"]["body"]


def test_jsonl_get_skill_unknown_returns_clean_error(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r3", "method": "kb.get_skill", "params": {"name": "missing"}},
    )
    assert out["ok"] is False
    assert "missing" in out["error"]["message"]


def test_jsonl_list_skills_empty_when_disabled(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(store.root / ".claude", "wired", body="# wired")
    _set_publish_skills(store, False)
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r4", "method": "kb.list_skills", "params": {}},
    )
    assert out["ok"] is True
    assert out["result"] == []


def test_jsonl_get_skill_permission_denied_when_disabled(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(store.root / ".claude", "wired", body="# wired\n\nbody")
    _set_publish_skills(store, False)
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r5", "method": "kb.get_skill", "params": {"name": "wired"}},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "permission_denied"


def test_jsonl_capabilities_reflects_gate(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "c1", "method": "kb.capabilities", "params": {}},
    )
    assert out["result"]["mcp"]["publish_skills"] is True
    _set_publish_skills(store, False)
    out = jsonl_server.handle_request(
        {"id": "c2", "method": "kb.capabilities", "params": {}},
    )
    assert out["result"]["mcp"]["publish_skills"] is False
