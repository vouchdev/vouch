"""Tests for the client-side diff/tree helpers in web/static/diff_view.js.

The module is a pure ES module (no imports), so node can execute it directly;
each test evaluates one expression and round-trips the result through JSON.
Skips when node isn't on PATH (GitHub CI runners ship it).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

STATIC = Path(__file__).resolve().parents[1] / "src" / "vouch" / "web" / "static"


def _eval(tmp_path: Path, expr: str) -> object:
    # node only parses .js as ESM inside a "type": "module" package, so the
    # module is copied next to the test as .mjs and imported by file url.
    mod = tmp_path / "diff_view.mjs"
    mod.write_text((STATIC / "diff_view.js").read_text())
    script = (
        f"import * as dv from {json.dumps(mod.as_uri())};\n"
        f"console.log(JSON.stringify({expr}));"
    )
    out = subprocess.run(
        [str(NODE), "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _parse(tmp_path: Path, diff: str) -> list:
    result = _eval(tmp_path, f"dv.parseDiff({json.dumps(diff)})")
    assert isinstance(result, list)
    return result


def _tree(tmp_path: Path, paths: list[str]) -> list:
    result = _eval(tmp_path, f"dv.buildFileTree({json.dumps(paths)})")
    assert isinstance(result, list)
    return result


def _flat(nodes: list, out: list[str] | None = None) -> list[str]:
    # flatten to "<type>:<path>" in render order, for compact assertions.
    if out is None:
        out = []
    for n in nodes:
        out.append(f"{n['type']}:{n['path']}")
        if n.get("children"):
            _flat(n["children"], out)
    return out


# --- parseDiff ---------------------------------------------------------------


def test_parse_diff_splits_per_file_and_drops_header_markers(tmp_path):
    d = "\n".join(
        [
            "diff --git a/src/x.py b/src/x.py",
            "index 111..222 100644",
            "--- a/src/x.py",
            "+++ b/src/x.py",
            "@@ -1,2 +1,2 @@",
            "-old",
            "+new",
        ]
    )
    files = _parse(tmp_path, d)
    assert len(files) == 1
    assert files[0]["path"] == "src/x.py"
    assert [f"{line['cls']}:{line['text']}" for line in files[0]["lines"]] == [
        "hunk:@@ -1,2 +1,2 @@",
        "del:-old",
        "add:+new",
    ]


def test_parse_diff_keeps_content_lines_starting_with_double_plus_or_minus(tmp_path):
    # these begin with +/- twice but are NOT file headers (no trailing space);
    # the old parser dropped them, hiding real changes from the diff view.
    d = "\n".join(
        [
            "diff --git a/c.py b/c.py",
            "--- a/c.py",
            "+++ b/c.py",
            "@@ -1,1 +1,2 @@",
            "++counter",
            "---flag",
        ]
    )
    lines = _parse(tmp_path, d)[0]["lines"]
    assert lines == [
        {"cls": "hunk", "text": "@@ -1,1 +1,2 @@"},
        {"cls": "add", "text": "++counter"},
        {"cls": "del", "text": "---flag"},
    ]


def test_parse_diff_empty_input(tmp_path):
    assert _parse(tmp_path, "") == []


def test_parse_diff_multi_file(tmp_path):
    d = "\n".join(
        [
            "diff --git a/a.py b/a.py",
            "@@ -1 +1 @@",
            "+a",
            "diff --git a/b.py b/b.py",
            "@@ -1 +1 @@",
            "+b",
        ]
    )
    assert [f["path"] for f in _parse(tmp_path, d)] == ["a.py", "b.py"]


# --- buildFileTree -----------------------------------------------------------


def test_tree_empty_input(tmp_path):
    assert _tree(tmp_path, []) == []


def test_tree_single_root_file_stays_flat(tmp_path):
    assert _tree(tmp_path, ["README.md"]) == [
        {"name": "README.md", "path": "README.md", "type": "blob"}
    ]


def test_tree_synthesizes_intermediate_directories(tmp_path):
    t = _tree(tmp_path, ["src/parser.py"])
    assert _flat(t) == ["tree:src", "blob:src/parser.py"]


def test_tree_sorts_folders_first_then_alphabetical(tmp_path):
    t = _tree(
        tmp_path,
        ["zeta.txt", "src/parser.py", "alpha.txt", "src/aaa.py", "docs/guide.md"],
    )
    assert _flat(t) == [
        "tree:docs",
        "blob:docs/guide.md",
        "tree:src",
        "blob:src/aaa.py",
        "blob:src/parser.py",
        "blob:alpha.txt",
        "blob:zeta.txt",
    ]


def test_tree_merges_shared_directory(tmp_path):
    t = _tree(tmp_path, ["src/a.py", "src/b.py"])
    assert len(t) == 1
    assert t[0]["path"] == "src"
    assert len(t[0]["children"]) == 2


def test_tree_dedupes_repeated_path(tmp_path):
    assert len(_tree(tmp_path, ["x.py", "x.py"])) == 1


def test_tree_deep_nesting(tmp_path):
    t = _tree(tmp_path, ["a/b/c/d.py"])
    assert _flat(t) == ["tree:a", "tree:a/b", "tree:a/b/c", "blob:a/b/c/d.py"]


# --- flattenTree (render order for the rail) ---------------------------------


def test_flatten_tree_render_rows_with_depth(tmp_path):
    rows = _eval(
        tmp_path,
        "dv.flattenTree(dv.buildFileTree(['src/a.py', 'src/sub/b.py', 'top.md']))",
    )
    assert [(r["type"], r["path"], r["depth"]) for r in rows] == [
        ("tree", "src", 0),
        ("tree", "src/sub", 1),
        ("blob", "src/sub/b.py", 2),
        ("blob", "src/a.py", 1),
        ("blob", "top.md", 0),
    ]
