"""Legacy integer "KB format" migrations.

This predates the semver model-schema runner (see :mod:`.runner`) and governs the
*directory layout* version stamped in ``config.yaml`` (``KB_FORMAT_VERSION``) —
making sure the ``.vouch/`` subdir tree and ``.gitignore`` exist. It is reached
by ``vouch migrate`` with no subcommand and is preserved verbatim; the semver
runner is a separate, additive concern keyed off ``.vouch/schema_version``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

from ..storage import KB_FORMAT_VERSION, SUBDIRS, KBStore, _starter_config


class MigrationError(RuntimeError):
    """Raised when a KB cannot be migrated safely."""


@dataclass(frozen=True)
class MigrationStep:
    from_version: int
    to_version: int
    description: str
    apply: Callable[[KBStore, bool], list[str]]


@dataclass(frozen=True)
class MigrationPlan:
    current_version: int
    target_version: int
    latest_version: int
    steps: list[MigrationStep]

    @property
    def needed(self) -> bool:
        return bool(self.steps)


@dataclass(frozen=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied: bool
    dry_run: bool
    steps: list[str]
    changes: list[str]


def read_config(store: KBStore) -> dict[str, Any]:
    if not store.config_path.exists():
        return {}
    loaded = yaml.safe_load(store.config_path.read_text())
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise MigrationError("config.yaml must contain a mapping")
    return loaded


def write_config(store: KBStore, config: dict[str, Any]) -> None:
    store.config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))


def detect_version(store: KBStore) -> int:
    config = read_config(store)
    raw = config.get("version", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MigrationError("config.yaml version must be an integer")
    if raw < 0:
        raise MigrationError("config.yaml version must be non-negative")
    if raw > KB_FORMAT_VERSION:
        raise MigrationError(
            f"KB format version {raw} is newer than this vouch supports "
            f"({KB_FORMAT_VERSION})"
        )
    return raw


def build_plan(store: KBStore, *, to_version: int | None = None) -> MigrationPlan:
    current = detect_version(store)
    target = KB_FORMAT_VERSION if to_version is None else to_version
    if target > KB_FORMAT_VERSION:
        raise MigrationError(
            f"target KB format version {target} is newer than this vouch supports "
            f"({KB_FORMAT_VERSION})"
        )
    if target < current:
        raise MigrationError(
            f"cannot migrate backwards from KB format version {current} to {target}"
        )

    steps: list[MigrationStep] = []
    version = current
    while version < target:
        step = _STEPS_BY_FROM.get(version)
        if step is None:
            raise MigrationError(f"no migration registered from KB format version {version}")
        if step.to_version > target:
            break
        steps.append(step)
        version = step.to_version

    if version != target:
        raise MigrationError(
            f"no complete migration path from KB format version {current} to {target}"
        )
    return MigrationPlan(
        current_version=current,
        target_version=target,
        latest_version=KB_FORMAT_VERSION,
        steps=steps,
    )


def migrate(
    store: KBStore,
    *,
    to_version: int | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    plan = build_plan(store, to_version=to_version)
    changes: list[str] = []
    for step in plan.steps:
        changes.extend(step.apply(store, dry_run))
    return MigrationResult(
        from_version=plan.current_version,
        to_version=plan.target_version,
        applied=plan.needed and not dry_run,
        dry_run=dry_run,
        steps=[s.description for s in plan.steps],
        changes=changes,
    )


def _migration_0_to_1(store: KBStore, dry_run: bool) -> list[str]:
    changes: list[str] = []

    missing_subdirs = [sub for sub in SUBDIRS if not (store.kb_dir / sub).is_dir()]
    for sub in missing_subdirs:
        changes.append(f"create {sub}/")
        if not dry_run:
            (store.kb_dir / sub).mkdir(parents=True, exist_ok=True)

    config = read_config(store)
    if not config:
        config = _starter_config()
        changes.append("create config.yaml with version 1")
    elif config.get("version") != 1:
        config = dict(config)
        config["version"] = 1
        changes.append("set config.yaml version to 1")
    if not dry_run and changes:
        write_config(store, config)

    gitignore_path = store.kb_dir / ".gitignore"
    required_ignores = ("proposed/", "state.db", "state.db-*")
    existing_ignores: list[str] = []
    if gitignore_path.exists():
        existing_ignores = gitignore_path.read_text().splitlines()
    missing_ignores = [line for line in required_ignores if line not in existing_ignores]
    if missing_ignores:
        changes.append("ensure .gitignore excludes proposed/ and state.db")
        if not dry_run:
            gitignore_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [*existing_ignores, *missing_ignores]
            gitignore_path.write_text("\n".join(lines).rstrip() + "\n")

    return changes


_STEPS: tuple[MigrationStep, ...] = (
    MigrationStep(
        from_version=0,
        to_version=1,
        description="stamp legacy KB as format version 1",
        apply=_migration_0_to_1,
    ),
)

_STEPS_BY_FROM = {step.from_version: step for step in _STEPS}
