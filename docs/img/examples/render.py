#!/usr/bin/env python3
"""Render terminal screenshots of the shipped example KBs as SVG.

Issue #286 asks for screenshots of the `examples/` fixtures in use so a
reader can see what vouch looks like before installing it. The issue
prefers *deterministic, regenerable* capture over hand-grabbed stills.

`vhs` (the tool used for `docs/demo.tape`) needs `ttyd` + `ffmpeg`, which
aren't always present. This generator needs only the Python stdlib and
the `vouch` CLI: it copies a shipped fixture to a throwaway `.vouch`,
runs each documented command with colour disabled, normalises the one
non-deterministic token (the absolute KB path), and renders the output
into a self-contained terminal-style SVG. SVG is text — it diffs cleanly
in PRs and re-renders byte-for-byte, so `tests/test_example_screenshots.py`
can assert the committed images are current.

Run from the repo root:

    python docs/img/examples/render.py

Images land in `docs/img/examples/`. Each is embedded in the matching
README under `examples/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = REPO_ROOT / "docs" / "img" / "examples"
# Stable stand-in for the throwaway working directory so output is
# reproducible no matter where the generator runs.
KB_PLACEHOLDER = "/your/project"


def _vouch_bin() -> str:
    """Resolve the `vouch` console script that matches the running interpreter.

    Prefer the `vouch` installed alongside this Python (the venv carrying the
    repo's editable build) over whatever is first on PATH. A stale global
    `vouch` shadowing the venv would otherwise render against the wrong build —
    silently overwriting the committed images with output from a different CLI.
    Fall back to PATH only when no interpreter-local script exists.
    """
    candidate = Path(sys.executable).parent / "vouch"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("vouch")
    if found:
        return found
    raise SystemExit("`vouch` console script not found; pip install -e '.[dev]' first")


@dataclass(frozen=True)
class Shot:
    name: str          # output basename (<name>.svg)
    fixture: str       # dir under examples/ holding a `vouch/` tree
    argv: list[str]    # vouch sub-command + args
    title: str         # window title-bar caption


SHOTS: list[Shot] = [
    # tiny/ — a complete four-claim KB with a full audit log.
    Shot("tiny-status", "tiny", ["status"], "tiny/ — vouch status"),
    Shot("tiny-search", "tiny", ["search", "auth"], "tiny/ — vouch search auth"),
    Shot("tiny-show", "tiny", ["show", "prop-001"], "tiny/ — vouch show prop-001"),
    Shot("tiny-audit", "tiny", ["audit"], "tiny/ — vouch audit"),
    # decision-log/ — demonstrates supersession across two pricing claims.
    Shot(
        "decision-log-search", "decision-log", ["search", "free-tier"],
        "decision-log/ — vouch search free-tier",
    ),
    Shot(
        "decision-log-diff", "decision-log",
        ["diff", "free-tier-100-req-superseded", "free-tier-500-req"],
        "decision-log/ — vouch diff (supersession)",
    ),
]


def capture(shot: Shot) -> list[str]:
    """Run one command against a copy of the fixture; return prompt + output
    lines with the volatile KB path normalised."""
    src = REPO_ROOT / "examples" / shot.fixture / "vouch"
    if not src.is_dir():
        raise SystemExit(f"missing fixture: {src}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(src, work / ".vouch")
        env = {
            **os.environ,
            "NO_COLOR": "1",
            "PYTHONUTF8": "1",
            "COLUMNS": "100",
        }
        proc = subprocess.run(
            [_vouch_bin(), *shot.argv],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            # Decode the child's UTF-8 output as UTF-8 regardless of the parent
            # locale, so output is byte-identical on a latin-1 box and in CI.
            encoding="utf-8",
        )
        raw = (proc.stdout + proc.stderr).rstrip("\n")
        # Normalise the throwaway working dir so output is location-independent.
        raw = raw.replace(str(work / ".vouch"), f"{KB_PLACEHOLDER}/.vouch")
        raw = raw.replace(str(work), KB_PLACEHOLDER)
        body = raw.split("\n") if raw else []
    return [f"$ vouch {' '.join(shot.argv)}", *body]


# --- SVG rendering --------------------------------------------------------

FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"
CHAR_W = 8.4       # advance width at FONT_SIZE
FONT_SIZE = 14
LINE_H = 20
PAD = 16
HEADER_H = 36
# GitHub-dark-ish palette.
BG = "#0d1117"
BAR = "#161b22"
FG = "#c9d1d9"
PROMPT = "#7ee787"
TITLE = "#8b949e"
DOTS = ("#ff5f56", "#ffbd2e", "#27c93f")


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(title: str, lines: list[str]) -> str:
    """Pure, deterministic: (title, lines) -> terminal-window SVG string."""
    widest = max(len(title) + 6, *(len(ln) for ln in lines)) if lines else len(title) + 6
    width = int(widest * CHAR_W) + 2 * PAD
    height = HEADER_H + len(lines) * LINE_H + PAD
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family="{FONT}" font-size="{FONT_SIZE}">',
        f'<rect width="{width}" height="{height}" rx="8" fill="{BG}"/>',
        f'<rect width="{width}" height="{HEADER_H}" rx="8" fill="{BAR}"/>',
        f'<rect y="{HEADER_H - 8}" width="{width}" height="8" fill="{BAR}"/>',
    ]
    for i, colour in enumerate(DOTS):
        parts.append(f'<circle cx="{PAD + i * 18}" cy="18" r="6" fill="{colour}"/>')
    parts.append(
        f'<text x="{width / 2}" y="23" fill="{TITLE}" text-anchor="middle" '
        f'>{_esc(title)}</text>'
    )
    y = HEADER_H + PAD
    for ln in lines:
        colour = PROMPT if ln.startswith("$ ") else FG
        parts.append(
            f'<text x="{PAD}" y="{y}" fill="{colour}" '
            f'xml:space="preserve">{_esc(ln)}</text>'
        )
        y += LINE_H
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def build(shot: Shot) -> str:
    return render_svg(shot.title, capture(shot))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for shot in SHOTS:
        svg = build(shot)
        (OUT_DIR / f"{shot.name}.svg").write_text(svg, encoding="utf-8")
        print(f"wrote docs/img/examples/{shot.name}.svg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
