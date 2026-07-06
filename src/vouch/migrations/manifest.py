"""Migration manifests — first-class yaml artifacts.

Each manifest declares a single consecutive version step for one artifact kind:

    from_version: "0.1.0"
    to_version:   "0.2.0"
    artifact:     claims
    description:  "rename confidence -> certainty"
    transforms:
      - rename: {from: confidence, to: certainty}
      - default: {field: scope, value: project}
    reverse:
      - rename: {from: certainty, to: confidence}

Manifests live in a repo-root ``migrations/`` directory (additive,
contributor-friendly, single-file PRs — same shape as ``adapters/``). The
``reverse`` block documents the inverse; rollback itself is content-based via the
journal, so it stays exact regardless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import semver
from .rewriter import ARTIFACT_KINDS, apply_transforms

VERBS = {"rename", "default", "drop", "split", "merge"}

#: Env override for the manifest directory (mainly for tests / vendored layouts).
MANIFESTS_DIR_ENV = "VOUCH_MIGRATIONS_DIR"


class ManifestError(RuntimeError):
    """A manifest file is malformed or the manifest set is inconsistent."""


@dataclass(frozen=True)
class Manifest:
    manifest_id: str  # filename stem, e.g. "0001-add-scope-spec"
    from_version: str
    to_version: str
    description: str
    artifact: str
    transforms: list[dict[str, Any]]
    reverse: list[dict[str, Any]]
    path: Path


def _validate_transforms(transforms: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(transforms, list):
        raise ManifestError(f"{where}: transforms must be a list")
    out: list[dict[str, Any]] = []
    for t in transforms:
        if not isinstance(t, dict) or len(t) != 1:
            raise ManifestError(f"{where}: each transform is a single-key mapping {{verb: spec}}")
        ((verb, _spec),) = t.items()
        if verb not in VERBS:
            raise ManifestError(
                f"{where}: unknown transform verb {verb!r} (allowed: {sorted(VERBS)})"
            )
        out.append(t)
    return out


def parse_manifest(path: Path) -> Manifest:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ManifestError(f"{path.name}: invalid yaml: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"{path.name}: manifest must be a mapping")
    for key in ("from_version", "to_version", "artifact"):
        if key not in data:
            raise ManifestError(f"{path.name}: missing required key {key!r}")
    from_v, to_v = str(data["from_version"]), str(data["to_version"])
    if not semver.is_valid(from_v) or not semver.is_valid(to_v):
        raise ManifestError(f"{path.name}: from_version/to_version must be MAJOR.MINOR.PATCH")
    if not semver.lt(from_v, to_v):
        raise ManifestError(f"{path.name}: to_version must be greater than from_version")
    artifact = str(data["artifact"])
    if artifact not in ARTIFACT_KINDS:
        raise ManifestError(
            f"{path.name}: unknown artifact {artifact!r} (allowed: {sorted(ARTIFACT_KINDS)})"
        )
    transforms = _validate_transforms(data.get("transforms", []), path.name)
    reverse = _validate_transforms(data.get("reverse", []), path.name)
    return Manifest(
        manifest_id=path.stem,
        from_version=from_v,
        to_version=to_v,
        description=str(data.get("description", "")),
        artifact=artifact,
        transforms=transforms,
        reverse=reverse,
        path=path,
    )


def load_manifests(manifests_dir: Path | None) -> list[Manifest]:
    """Parse every ``*.yaml`` manifest in the directory, sorted by filename."""
    if manifests_dir is None or not manifests_dir.is_dir():
        return []
    out = [parse_manifest(p) for p in sorted(manifests_dir.glob("*.yaml"))]
    _check_no_duplicate_steps(out)
    return out


def _check_no_duplicate_steps(manifests: list[Manifest]) -> None:
    seen: dict[str, str] = {}
    for m in manifests:
        if m.from_version in seen:
            raise ManifestError(
                f"two manifests migrate from {m.from_version}: "
                f"{seen[m.from_version]} and {m.manifest_id}"
            )
        seen[m.from_version] = m.manifest_id


def default_manifests_dir() -> Path | None:
    """Resolve the manifest directory: env override, else repo-root ``migrations/``.

    Walks up from this package looking for a directory that holds both
    ``pyproject.toml`` and a ``migrations/`` subdir (a source checkout). Returns
    ``None`` when neither is found (e.g. an installed wheel with no manifests yet).
    """
    env = os.environ.get(MANIFESTS_DIR_ENV)
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "migrations"
        if (parent / "pyproject.toml").is_file() and candidate.is_dir():
            return candidate
    return None


# Re-exported so callers don't need a separate import to preview a transform.
__all__ = [
    "VERBS",
    "Manifest",
    "ManifestError",
    "apply_transforms",
    "default_manifests_dir",
    "load_manifests",
    "parse_manifest",
]
