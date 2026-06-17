"""Browser-based review console for vouch.

The web layer is a *viewport* over the existing kb.* surface — every action
(approve, reject, contradict) goes through the same ``vouch.proposals`` /
``vouch.lifecycle`` / ``vouch.storage`` code path as the CLI, so the audit log
is identical regardless of surface.

The dependencies (fastapi, jinja2) live behind the ``[web]`` extra so the
base install stays light. ``vouch review-ui`` produces an actionable
``ImportError`` line if the extra is missing.
"""

from __future__ import annotations


def _require_web_extra() -> None:
    """Fail with a clean message if fastapi/jinja2 aren't installed."""
    missing: list[str] = []
    try:
        import fastapi  # noqa: F401
    except ImportError:
        missing.append("fastapi")
    try:
        import jinja2  # noqa: F401
    except ImportError:
        missing.append("jinja2")
    if missing:
        raise ImportError(
            "vouch review-ui needs the [web] extra. "
            "Install with: pip install 'vouch-kb[web]'  "
            f"(missing: {', '.join(missing)})"
        )


def create_app(  # type: ignore[no-untyped-def]
    kb_root: str | None = None,
    *,
    auth_token: str | None = None,
    auth_label: str = "web-reviewer",
    page_size: int | None = None,
):
    """Build the FastAPI app for a given KB root. Lazy-imports the web stack.

    ``auth_token`` enables the Bearer gate (every route requires the token);
    ``auth_label`` is the reviewer identity recorded in the audit log for
    token-authenticated actions. ``page_size`` overrides queue pagination.
    """
    _require_web_extra()
    from .server import DEFAULT_PAGE_SIZE, AuthConfig, build_app

    auth = AuthConfig(token=auth_token, label=auth_label)
    return build_app(
        kb_root,
        auth=auth,
        page_size=page_size if page_size is not None else DEFAULT_PAGE_SIZE,
    )


__all__ = ["create_app"]
