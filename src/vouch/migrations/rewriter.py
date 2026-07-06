"""Atomic file rewrites and the manifest transform verbs.

Every mutation a migration performs goes through :func:`atomic_write_text`:
write to a temp file in the same directory, ``fsync``, then ``os.replace`` (an
atomic rename on POSIX). A crash therefore never leaves a half-written artifact —
a file is either its old bytes or its new bytes, never a torn mix. This mirrors
the audit log's own fsync-on-append durability; ``KBStore`` exposes no shared
atomic-write helper, so the migration layer provides its own.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..storage import _FRONTMATTER_RE, _yaml_dump, _yaml_load

# Artifact kinds a manifest may target, mapped to (subdir, on-disk format).
ARTIFACT_KINDS: dict[str, tuple[str, str]] = {
    "claims": ("claims", "yaml"),
    "entities": ("entities", "yaml"),
    "relations": ("relations", "yaml"),
    "evidence": ("evidence", "yaml"),
    "sessions": ("sessions", "yaml"),
    "pages": ("pages", "md"),
}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mig-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave no temp files behind on failure (incl. KeyboardInterrupt).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- transform verbs -------------------------------------------------------


def _t_rename(d: dict, spec: dict) -> None:
    frm, to = spec["from"], spec["to"]
    if frm in d:
        d[to] = d.pop(frm)


def _t_default(d: dict, spec: dict) -> None:
    field = spec["field"]
    if field not in d:
        d[field] = spec["value"]


def _t_drop(d: dict, spec: dict) -> None:
    d.pop(spec["field"], None)


def _t_split(d: dict, spec: dict) -> None:
    field = spec["field"]
    into = list(spec["into"])
    sep = spec.get("on", " ")
    if field not in d:
        return
    parts = str(d[field]).split(sep)
    for i, name in enumerate(into):
        d[name] = parts[i] if i < len(parts) else ""
    if field not in into:
        del d[field]


def _t_merge(d: dict, spec: dict) -> None:
    fields = list(spec["fields"])
    into = spec["into"]
    joiner = spec.get("with", " ")
    values = [str(d.get(f, "")) for f in fields]
    d[into] = joiner.join(values)
    for f in fields:
        if f != into:
            d.pop(f, None)


_VERB_FNS = {
    "rename": _t_rename,
    "default": _t_default,
    "drop": _t_drop,
    "split": _t_split,
    "merge": _t_merge,
}


def apply_transforms(data: dict, transforms: list[dict]) -> dict:
    """Return a new dict with each transform applied in order."""
    out = dict(data)
    for transform in transforms:
        # Each transform is a single-key mapping: {verb: spec}.
        ((verb, spec),) = transform.items()
        _VERB_FNS[verb](out, spec)
    return out


# --- per-file content transforms ------------------------------------------


def artifact_files(kb_dir: Path, kind: str) -> list[Path]:
    subdir, fmt = ARTIFACT_KINDS[kind]
    ext = "*.md" if fmt == "md" else "*.yaml"
    return sorted((kb_dir / subdir).glob(ext))


def transform_text(text: str, kind: str, transforms: list[dict]) -> str:
    """Apply a manifest's transforms to one artifact file's text."""
    fmt = ARTIFACT_KINDS[kind][1]
    if fmt == "yaml":
        data = _yaml_load(text)
        if not isinstance(data, dict):
            return text
        return _yaml_dump(apply_transforms(data, transforms))
    # markdown page: transform the YAML frontmatter, leave the body untouched.
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    front = _yaml_load(match.group(1)) or {}
    if not isinstance(front, dict):
        return text
    body = match.group(2)
    new_front = _yaml_dump(apply_transforms(front, transforms)).rstrip("\n")
    return f"---\n{new_front}\n---\n{body}"
