"""Idempotently install vouch into an MCP-aware host's project tree.

Each supported host ships a templates directory under ``adapters/<name>/``
plus an ``install.yaml`` manifest describing which templates land at which
paths under which adoption tier. The writer reads the manifest and copies
files into ``target``:

* **Missing dest** -> the file is created (recorded in ``InstallResult.written``).
* **Existing dest** -> the file is left alone (``InstallResult.skipped``).
* **CLAUDE.md with ``fenced_append``** -> if the destination already exists
  AND doesn't already contain the fence markers, the snippet is appended
  inside a ``<!-- BEGIN vouch --> ... <!-- END vouch -->`` block
  (``InstallResult.appended``). If the fence already exists, the file is
  treated as skipped -- so reruns of ``vouch install-mcp`` stay flat-noop.

Tiers stack from T1 (the minimum: MCP wire) through T4 (full integration:
slash commands and host-side hooks). Each manifest declares only the tiers
its host has templates for; hosts without T3/T4 surfaces (most non-Claude
ones) simply omit those keys and the writer treats them as no-ops.

Why YAML manifests, not hard-coded Python: every new host should be a
``adapters/<name>/`` directory + an ``install.yaml`` -- a single-file PR by
a contributor who knows that host, not a code change to ``install_adapter.py``.
The dictionary-driven approach also makes it trivial to inspect what an
adapter will do (``cat adapters/<name>/install.yaml``).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Adapters live at ``<repo>/adapters/`` in a checkout / editable install,
# and are force-included into the wheel at ``vouch/adapters/`` (see
# pyproject.toml) so pip/pipx installs ship them too. Prefer the repo copy
# when present so dev edits win over a stale packaged copy.
_REPO_ADAPTERS = Path(__file__).resolve().parent.parent.parent / "adapters"
_PACKAGED_ADAPTERS = Path(__file__).resolve().parent / "adapters"
ADAPTERS_DIR = _REPO_ADAPTERS if _REPO_ADAPTERS.is_dir() else _PACKAGED_ADAPTERS

_TIER_ORDER: tuple[str, ...] = ("T1", "T2", "T3", "T4")
_DEFAULT_FENCE_BEGIN = "<!-- BEGIN vouch -->"
_DEFAULT_FENCE_END = "<!-- END vouch -->"


class AdapterError(RuntimeError):
    """Raised for user-visible adapter problems (unknown host, bad tier,
    malformed manifest). The CLI layer translates this into a clean
    ``Error: ...`` line via the existing ``_cli_errors`` context manager."""


@dataclass
class InstallResult:
    """Outcome of an :func:`install` call, partitioned by what happened to
    each declared file.

    Paths are reported relative to ``target`` so the values are stable across
    different absolute install locations -- callers / tests can compare them
    directly without resolving against ``tmp_path``.
    """
    written: list[str] = field(default_factory=list)
    appended: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _FileEntry:
    src: str           # path relative to the adapter directory
    dst: str           # path relative to the target directory
    fenced_append: bool = False  # CLAUDE.md-style: append inside our fence
    json_merge: bool = False  # settings.json-style: deep-merge into existing


@dataclass(frozen=True)
class _Manifest:
    host: str
    pretty: str
    tiers: dict[str, list[_FileEntry]]
    fence_begin: str = _DEFAULT_FENCE_BEGIN
    fence_end: str = _DEFAULT_FENCE_END


def _load_manifest(host: str) -> _Manifest:
    manifest_path = ADAPTERS_DIR / host / "install.yaml"
    if not manifest_path.is_file():
        raise AdapterError(
            f"adapter {host!r} has no install.yaml at {manifest_path}"
        )
    try:
        data: Any = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise AdapterError(f"{host}: install.yaml is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise AdapterError(f"{host}: install.yaml must be a YAML mapping at the top level")
    if data.get("host") != host:
        raise AdapterError(
            f"{host}: install.yaml `host:` field is {data.get('host')!r}, "
            f"expected {host!r} (must match directory name)"
        )
    raw_tiers = data.get("tiers") or {}
    if not isinstance(raw_tiers, dict):
        raise AdapterError(f"{host}: install.yaml `tiers:` must be a mapping")

    parsed: dict[str, list[_FileEntry]] = {}
    for tier_name, entries in raw_tiers.items():
        if tier_name not in _TIER_ORDER:
            raise AdapterError(
                f"{host}: install.yaml declares unknown tier {tier_name!r} "
                f"(valid: {', '.join(_TIER_ORDER)})"
            )
        if not isinstance(entries, list):
            raise AdapterError(
                f"{host}: install.yaml tier {tier_name} must be a list of file entries"
            )
        parsed_entries: list[_FileEntry] = []
        for raw in entries:
            if not isinstance(raw, dict):
                raise AdapterError(
                    f"{host}: install.yaml tier {tier_name} entry must be a mapping, "
                    f"got {type(raw).__name__}"
                )
            src = raw.get("src")
            dst = raw.get("dst")
            if not isinstance(src, str) or not src.strip():
                raise AdapterError(
                    f"{host}: install.yaml tier {tier_name}: every entry needs a non-empty `src`"
                )
            if not isinstance(dst, str) or not dst.strip():
                raise AdapterError(
                    f"{host}: install.yaml tier {tier_name}: every entry needs a non-empty `dst`"
                )
            fenced = bool(raw.get("fenced_append", False))
            json_merge = bool(raw.get("json_merge", False))
            parsed_entries.append(
                _FileEntry(src=src, dst=dst, fenced_append=fenced, json_merge=json_merge)
            )
        if parsed_entries:
            parsed[tier_name] = parsed_entries

    if not parsed:
        raise AdapterError(f"{host}: install.yaml declares zero usable tiers")

    fence = data.get("fence") or {}
    if isinstance(fence, dict):
        fence_begin = fence.get("begin", _DEFAULT_FENCE_BEGIN)
        fence_end = fence.get("end", _DEFAULT_FENCE_END)
    else:
        fence_begin = _DEFAULT_FENCE_BEGIN
        fence_end = _DEFAULT_FENCE_END

    return _Manifest(
        host=host,
        pretty=str(data.get("pretty") or host),
        tiers=parsed,
        fence_begin=fence_begin,
        fence_end=fence_end,
    )


def available_adapters() -> list[str]:
    """Every directory under ``adapters/`` that has an ``install.yaml``."""
    if not ADAPTERS_DIR.is_dir():
        return []
    out: list[str] = []
    for p in ADAPTERS_DIR.iterdir():
        if not p.is_dir():
            continue
        if (p / "install.yaml").is_file():
            out.append(p.name)
    return sorted(out)


def install(adapter: str, *, target: Path, tier: str = "T4") -> InstallResult:
    """Install ``adapter``'s templates under ``target`` up to ``tier``.

    The call is idempotent: rerunning against a previously-installed tree
    produces an :class:`InstallResult` with everything in ``skipped`` and
    nothing in ``written`` / ``appended``.
    """
    if tier not in _TIER_ORDER:
        raise AdapterError(
            f"unknown tier {tier!r} (valid: {', '.join(_TIER_ORDER)})"
        )
    if adapter not in available_adapters():
        raise AdapterError(
            f"unknown adapter {adapter!r} "
            f"(available: {', '.join(available_adapters()) or '(none)'})"
        )

    manifest = _load_manifest(adapter)
    src_root = ADAPTERS_DIR / adapter
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    result = InstallResult()
    selected_tiers = _TIER_ORDER[: _TIER_ORDER.index(tier) + 1]

    for tier_name in selected_tiers:
        entries = manifest.tiers.get(tier_name, [])
        for entry in entries:
            src = src_root / entry.src
            dst = target / entry.dst
            if not src.is_file():
                # Manifest declares a template that doesn't exist in the
                # adapter directory: a contributor-time bug, but surface it
                # at install-time too with the file path so it's obvious
                # what to fix.
                raise AdapterError(
                    f"{adapter}: install.yaml declares src {entry.src!r} "
                    f"but {src} is not a file"
                )

            if entry.fenced_append:
                _install_fenced(src, dst, manifest, result, entry.dst)
                continue

            if entry.json_merge:
                _install_json_merge(src, dst, result, entry.dst)
                continue

            if dst.exists():
                result.skipped.append(entry.dst)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result.written.append(entry.dst)

    return result


def _install_fenced(
    src: Path,
    dst: Path,
    manifest: _Manifest,
    result: InstallResult,
    rel_dst: str,
) -> None:
    """CLAUDE.md-style: a snippet that lives inside a fence so re-runs are
    flat-noop and user content above/below the fence is untouched.

    States:

    * dst is missing                 -> write fresh, fenced (``written``)
    * dst exists, fence not in file  -> append fenced block (``appended``)
    * dst exists, fence already in   -> skip (``skipped``); we are the
                                        author and there's nothing to do
    """
    snippet = src.read_text(encoding="utf-8")
    fenced_block = f"\n{manifest.fence_begin}\n{snippet.rstrip()}\n{manifest.fence_end}\n"

    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(fenced_block.lstrip("\n"), encoding="utf-8")
        result.written.append(rel_dst)
        return

    existing = dst.read_text(encoding="utf-8")
    if manifest.fence_begin in existing:
        result.skipped.append(rel_dst)
        return

    # User-authored content above; append our fenced block at the bottom.
    new_content = existing.rstrip() + "\n" + fenced_block
    dst.write_text(new_content, encoding="utf-8")
    result.appended.append(rel_dst)


def _event_commands(groups: Any) -> set[str]:
    """Every hook ``command`` string already present under one hooks-event."""
    cmds: set[str] = set()
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []) or []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                cmds.add(hook["command"])
    return cmds


def _merge_settings(src: dict[str, Any], dst: dict[str, Any]) -> bool:
    """Merge our ``permissions.allow`` + ``hooks`` into an existing settings
    dict in place. Returns True if ``dst`` changed. Idempotent: re-merging the
    same ``src`` is a no-op because every command / permission is deduped.
    """
    changed = False

    # permissions.allow — union, preserving the user's order.
    src_perms = src.get("permissions")
    if isinstance(src_perms, dict) and isinstance(src_perms.get("allow"), list):
        dst_perms = dst.get("permissions")
        if not isinstance(dst_perms, dict):
            dst_perms = {}
            dst["permissions"] = dst_perms
        dst_allow = dst_perms.get("allow")
        if not isinstance(dst_allow, list):
            dst_allow = []
            dst_perms["allow"] = dst_allow
        seen = set(dst_allow)
        for item in src_perms["allow"]:
            if item not in seen:
                dst_allow.append(item)
                seen.add(item)
                changed = True

    # hooks — per event, add only commands not already present. Prefer folding
    # into an existing group with the same matcher so we don't fan out groups.
    src_hooks = src.get("hooks")
    if isinstance(src_hooks, dict):
        dst_hooks = dst.get("hooks")
        if not isinstance(dst_hooks, dict):
            dst_hooks = {}
            dst["hooks"] = dst_hooks
        for event, src_groups in src_hooks.items():
            if not isinstance(src_groups, list):
                continue
            dst_groups = dst_hooks.get(event)
            if not isinstance(dst_groups, list):
                dst_groups = []
                dst_hooks[event] = dst_groups
            present = _event_commands(dst_groups)
            for group in src_groups:
                if not isinstance(group, dict):
                    continue
                fresh = [
                    hook for hook in group.get("hooks", []) or []
                    if isinstance(hook, dict) and hook.get("command") not in present
                ]
                if not fresh:
                    continue
                matcher = group.get("matcher")
                target_group = next(
                    (g for g in dst_groups
                     if isinstance(g, dict) and g.get("matcher") == matcher),
                    None,
                )
                if target_group is not None:
                    target_group.setdefault("hooks", []).extend(fresh)
                else:
                    new_group = {k: v for k, v in group.items() if k != "hooks"}
                    new_group["hooks"] = fresh
                    dst_groups.append(new_group)
                present.update(
                    h["command"] for h in fresh if isinstance(h.get("command"), str)
                )
                changed = True

    return changed


def _install_json_merge(
    src: Path, dst: Path, result: InstallResult, rel_dst: str
) -> None:
    """settings.json-style: deep-merge our hooks + permissions into a
    pre-existing JSON file instead of skipping it.

    States:

    * dst missing                 -> copy fresh (``written``)
    * dst exists, merge adds keys -> merge + rewrite (``merged``)
    * dst exists, nothing to add  -> skip (``skipped``); already installed
    * dst exists, unparseable     -> skip (``skipped``); never clobber the user
    """
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        result.written.append(rel_dst)
        return

    try:
        dst_data = json.loads(dst.read_text(encoding="utf-8"))
        src_data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Malformed or unreadable user file — leave it untouched.
        result.skipped.append(rel_dst)
        return
    if not isinstance(dst_data, dict) or not isinstance(src_data, dict):
        result.skipped.append(rel_dst)
        return

    if _merge_settings(src_data, dst_data):
        dst.write_text(json.dumps(dst_data, indent=2) + "\n", encoding="utf-8")
        result.merged.append(rel_dst)
    else:
        result.skipped.append(rel_dst)
