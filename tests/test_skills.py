"""kb.list_skills / kb.get_skill — Claude Code skill discovery over MCP."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vouch import skills as skills_mod
from vouch.mcp_config import load_config
from vouch.storage import KBStore


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KBStore:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return KBStore.init(tmp_path / "kb")


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


def _set_publish_skills(store: KBStore, value: bool) -> None:
    cfg_path = store.kb_dir / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["mcp"] = {"publish_skills": value}
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def test_list_skills_scans_project_local(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "vouch-recall", body="# vouch-recall\n\nCall kb.recall.")
    rows = skills_mod.list_skills(store)
    names = [r["name"] for r in rows]
    assert "vouch-recall" in names
    rec = next(r for r in rows if r["name"] == "vouch-recall")
    assert rec["scope"] == "project"
    assert rec["kind"] == "skill"
    assert rec["description"] == "this is the vouch-recall skill"


def test_list_skills_scans_user_global(store: KBStore) -> None:
    home = Path.home()
    _write_skill(home / ".claude", "global-skill", body="# global-skill\n\nbody")
    rows = skills_mod.list_skills(store)
    rec = next(r for r in rows if r["name"] == "global-skill")
    assert rec["scope"] == "user"


def test_project_overrides_user_on_collision(store: KBStore) -> None:
    home = Path.home()
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
    assert skills_mod.list_skills(store) == []


def test_unreadable_skill_does_not_break_listing(store: KBStore) -> None:
    skill_dir = store.root / ".claude" / "skills" / "good"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: good\n---\n\nbody", encoding="utf-8")
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


# --- publish_skills gate (#235) -------------------------------------------


def test_mcp_config_defaults_publish_skills_true(store: KBStore) -> None:
    assert load_config(store).publish_skills is True


def test_list_skills_empty_when_publish_skills_false(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "hidden", body="# hidden")
    _set_publish_skills(store, False)
    assert skills_mod.list_skills(store) == []


def test_get_skill_permission_denied_when_publish_skills_false(store: KBStore) -> None:
    from vouch.skills.errors import SkillsAccessDenied

    _write_skill(store.root / ".claude", "hidden", body="# hidden")
    _set_publish_skills(store, False)
    with pytest.raises(SkillsAccessDenied):
        skills_mod.get_skill(store, "hidden")


def test_flipping_publish_skills_takes_effect_immediately(store: KBStore) -> None:
    _write_skill(store.root / ".claude", "toggle", body="# toggle")
    assert len(skills_mod.list_skills(store)) == 1
    _set_publish_skills(store, False)
    assert skills_mod.list_skills(store) == []
    _set_publish_skills(store, True)
    assert len(skills_mod.list_skills(store)) == 1


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


def test_jsonl_get_skill_permission_denied_when_gated(
    store: KBStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_skill(store.root / ".claude", "secret", body="# secret")
    _set_publish_skills(store, False)
    monkeypatch.chdir(store.root)
    from vouch import jsonl_server

    out = jsonl_server.handle_request(
        {"id": "r4", "method": "kb.get_skill", "params": {"name": "secret"}},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "permission_denied"


def test_capabilities_surfaces_publish_skills_flag(store: KBStore) -> None:
    from vouch.capabilities import capabilities

    assert capabilities(store).mcp["publish_skills"] is True
    _set_publish_skills(store, False)
    assert capabilities(store).mcp["publish_skills"] is False
