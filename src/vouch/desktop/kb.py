"""KB folder validation and init helpers for the desktop shell."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import audit as audit_mod
from .. import health
from ..onboarding import seed_starter_kb
from ..storage import KB_DIRNAME, KBStore, discover_root


@dataclass(frozen=True)
class KbCheckResult:
    ok: bool
    project_root: str | None
    kb_dir: str | None
    message: str

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "ok": self.ok,
            "project_root": self.project_root,
            "kb_dir": self.kb_dir,
            "message": self.message,
        }


def kb_label(project_root: str | Path) -> str:
    """Short display label for a project root (basename)."""
    root = Path(project_root).resolve()
    return root.name or str(root)


def _resolve_selection(selected: Path) -> Path:
    """Accept either a project root or a ``.vouch`` directory from a picker."""
    resolved = selected.resolve()
    if resolved.name == KB_DIRNAME and resolved.is_dir():
        return resolved.parent
    return resolved


def check_kb_folder(selected: str | Path) -> KbCheckResult:
    """Validate that ``selected`` contains (or is) a ``.vouch/`` directory."""
    try:
        root = _resolve_selection(Path(selected))
    except OSError as e:
        return KbCheckResult(
            ok=False,
            project_root=None,
            kb_dir=None,
            message=str(e),
        )

    if not root.is_dir():
        return KbCheckResult(
            ok=False,
            project_root=str(root),
            kb_dir=None,
            message=f"{root!s} is not a directory",
        )

    kb_dir = root / KB_DIRNAME
    if kb_dir.is_dir():
        return KbCheckResult(
            ok=True,
            project_root=str(root),
            kb_dir=str(kb_dir),
            message="ok",
        )

    return KbCheckResult(
        ok=False,
        project_root=str(root),
        kb_dir=str(kb_dir),
        message=f"no {KB_DIRNAME}/ directory at {root}",
    )


def init_kb_at(
    selected: str | Path,
    *,
    actor: str = "desktop",
) -> dict[str, str | bool]:
    """Run ``vouch init`` semantics at ``selected``; return a JSON-friendly dict."""
    root = _resolve_selection(Path(selected))
    root.mkdir(parents=True, exist_ok=True)
    store = KBStore.init(root)
    seed = seed_starter_kb(store, approved_by=actor)
    health.rebuild_index(store)
    audit_mod.log_event(store.kb_dir, event="kb.init", actor=actor)
    status = health.status(store)
    has_starter = bool(status.get("claims", 0) >= 1)
    return {
        "ok": True,
        "project_root": str(store.root),
        "kb_dir": str(store.kb_dir),
        "claim_id": seed.claim_id if seed.created_anything else "",
        "starter_present": has_starter,
        "label": kb_label(store.root),
    }


def discover_from_path(start: str | Path) -> KbCheckResult:
    """Walk up from ``start`` like the CLI ``discover`` command."""
    try:
        root = discover_root(Path(start))
    except Exception as e:
        return KbCheckResult(
            ok=False,
            project_root=None,
            kb_dir=None,
            message=str(e),
        )
    return KbCheckResult(
        ok=True,
        project_root=str(root),
        kb_dir=str(root / KB_DIRNAME),
        message="ok",
    )
