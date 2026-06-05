"""Review-gate configuration helpers shared by transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _review_config(kb_dir: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load((kb_dir / "config.yaml").read_text())
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    review = loaded.get("review")
    return review if isinstance(review, dict) else {}


def decision_tools_enabled(kb_dir: Path) -> bool:
    review = _review_config(kb_dir)
    return (
        review.get("expose_decision_tools") is True
        or review.get("approver_role") == "trusted-agent"
    )


def require_decision_tools_enabled(kb_dir: Path) -> None:
    if decision_tools_enabled(kb_dir):
        return
    raise ValueError(
        "kb.approve/kb.reject are disabled on agent transports by default; "
        "use the human CLI (`vouch approve` / `vouch reject`) or set "
        "review.expose_decision_tools: true for a trusted host"
    )
