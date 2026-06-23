#!/usr/bin/env python3
"""Regenerate requirements-flatpak.txt from pyproject.toml (#211)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
OUT = Path(__file__).resolve().parents[1] / "requirements-flatpak.txt"

HEADER = """\
# Pinned runtime dependencies for the Flatpak [web] install path (#211).
# Mirrors pyproject.toml core + web extras; regenerate with:
#   desktop/flatpak/scripts/generate-requirements.py

"""


def main() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    # project.dependencies is under [project] not optional
    proj = re.search(r"dependencies = \[(.*?)\]", text, re.DOTALL)
    deps: list[str] = []
    if proj:
        deps.extend(re.findall(r'"([^"]+)"', proj.group(1)))
    web_block = re.search(
        r"web = \[(.*?)\]",
        text,
        re.DOTALL,
    )
    if web_block:
        deps.extend(re.findall(r'"([^"]+)"', web_block.group(1)))

    # de-dupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for d in deps:
        if d not in seen:
            seen.add(d)
            unique.append(d)

    OUT.write_text(HEADER + "\n".join(unique) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(unique)} deps)")


if __name__ == "__main__":
    main()
