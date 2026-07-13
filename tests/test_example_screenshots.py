"""The example-KB screenshots (issue #286) stay reproducible and current.

`docs/img/examples/render.py` renders deterministic terminal SVGs from the
shipped `examples/` fixtures. These tests assert the renderer is pure, escapes
XML, and — the part that matters for a docs PR — that the committed `.svg`
files are byte-identical to a fresh render. If a fixture or the CLI output
format drifts, this fails with a pointer to `make examples-screenshots`.

Well-formedness is checked structurally rather than with a stdlib XML parser:
the SVGs are self-generated and contain no DTD or entities, so a parser-free
check avoids the XXE / entity-expansion surface entirely.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_PY = REPO_ROOT / "docs" / "img" / "examples" / "render.py"


def _load_render():
    spec = importlib.util.spec_from_file_location("example_render", RENDER_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its own
    # module via sys.modules (required on Python 3.14).
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


render = _load_render()


def _vouch_available() -> bool:
    if shutil.which("vouch"):
        return True
    return (Path(sys.executable).parent / "vouch").exists()


def _assert_well_formed_svg(text: str) -> None:
    # Self-generated content: no DTD/entities, so no parser (and no XXE) needed.
    assert "<!DOCTYPE" not in text and "<!ENTITY" not in text
    assert text.startswith("<svg ")
    assert text.rstrip().endswith("</svg>")
    assert text.count("<text") == text.count("</text>")
    assert text.count("<svg") == text.count("</svg>") == 1


def test_render_svg_is_pure_and_deterministic() -> None:
    lines = ["$ vouch status", "KB at /your/project/.vouch", "  durable: 4 claims"]
    a = render.render_svg("title", lines)
    b = render.render_svg("title", lines)
    assert a == b
    _assert_well_formed_svg(a)


def test_render_svg_escapes_xml() -> None:
    svg = render.render_svg("t & <x>", ['a & b < c > d "q"'])
    assert "&amp;" in svg and "&lt;" in svg and "&gt;" in svg and "&quot;" in svg
    # No raw, unescaped metacharacters leaked into text content.
    assert "a & b" not in svg
    _assert_well_formed_svg(svg)


def test_committed_svgs_are_well_formed() -> None:
    for shot in render.SHOTS:
        path = render.OUT_DIR / f"{shot.name}.svg"
        assert path.is_file(), f"missing committed screenshot: {path}"
        _assert_well_formed_svg(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not _vouch_available(), reason="vouch console script not installed")
def test_committed_svgs_match_fresh_render() -> None:
    """Committed images reproduce exactly — keeps docs honest with the code."""
    for shot in render.SHOTS:
        committed = (render.OUT_DIR / f"{shot.name}.svg").read_text(encoding="utf-8")
        fresh = render.build(shot)
        assert committed == fresh, (
            f"{shot.name}.svg is stale — run `make examples-screenshots` "
            f"and commit the result"
        )
