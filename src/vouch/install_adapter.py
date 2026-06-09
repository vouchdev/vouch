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

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# All adapters live next to the package, in ``<repo>/adapters/``. The writer
# resolves the location from this file's path so it works equally for an
# editable install, a wheel layout, and a sdist test environment.
ADAPTERS_DIR = Path(__file__).resolve().parent.parent.parent / "adapters"

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


@dataclass(frozen=True)
class _FileEntry:
    src: str           # path relative to the adapter directory
    dst: str           # path relative to the target directory
    fenced_append: bool = False  # CLAUDE.md-style: append inside our fence


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
            parsed_entries.append(_FileEntry(src=src, dst=dst, fenced_append=fenced))
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
