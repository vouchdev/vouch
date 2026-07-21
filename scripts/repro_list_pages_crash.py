#!/usr/bin/env python3
"""Proof for PR #360: one corrupt pages/*.md crashes bulk listing on main.

Run from the repo root::

    python scripts/repro_list_pages_crash.py

On **main** (before the fix), ``store.list_pages()`` raises::

    ValueError: page file missing YAML frontmatter

That exception propagates through ``health.lint()``, ``health.status()``,
``kb.list_pages``, and any other caller of ``list_pages()`` — one bad file
takes down the whole KB listing surface.

With the fix branch applied, ``list_pages()`` logs a warning and returns
only the readable pages.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from vouch import health
from vouch.models import Claim, Page, PageType
from vouch.storage import KBStore, _deserialize_page


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="vouch-repro-list-pages-"))
    try:
        store = KBStore.init(root)
        src = store.put_source(b"evidence")
        store.put_claim(Claim(id="c1", text="fact", evidence=[src.id]))
        store.put_page(Page(id="good-page", title="Good", body="ok", type=PageType.CONCEPT))
        bad_path = store.kb_dir / "pages" / "bad-page.md"
        bad_path.write_text("not valid frontmatter — no YAML block", encoding="utf-8")

        print("=== 1. Direct deserialize (always fails on corrupt file) ===")
        try:
            _deserialize_page(bad_path.read_text(encoding="utf-8"))
            print("unexpected: _deserialize_page succeeded")
        except ValueError as e:
            print(f"ValueError: {e}")

        print("\n=== 2. store.list_pages() ===")
        try:
            pages = store.list_pages()
            print(f"OK — returned {len(pages)} page(s): {[p.id for p in pages]}")
            print("(fix branch: bad-page.md skipped with a logged warning)")
        except ValueError as e:
            print(f"CRASH — ValueError: {e}")
            print("(main branch: entire listing aborts here)")

        print("\n=== 3. health.lint(store) ===")
        try:
            report = health.lint(store)
            print(f"OK — lint finished, ok={report.ok}, findings={len(report.findings)}")
        except ValueError as e:
            print(f"CRASH — ValueError: {e}")

        print("\n=== 4. health.status(store) ===")
        try:
            summary = health.status(store)
            print(f"OK — pages={summary['pages']}")
        except ValueError as e:
            print(f"CRASH — ValueError: {e}")

        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
