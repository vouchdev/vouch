"""Bundle export + import + verify round-trip benchmarks."""
from __future__ import annotations

from pathlib import Path

import pytest

from vouch import bundle
from vouch.health import rebuild_index
from vouch.storage import KBStore


@pytest.fixture(scope="module")
def exported_bundle_1k(kb_1k, tmp_path_factory):
    store = KBStore(kb_1k)
    rebuild_index(store)
    dest = tmp_path_factory.mktemp("bundles") / "bench_1k.tar.gz"
    bundle.export(store.kb_dir, dest=dest, actor="bench")
    return dest


@pytest.fixture(scope="module")
def exported_bundle_10k(kb_10k, tmp_path_factory):
    store = KBStore(kb_10k)
    rebuild_index(store)
    dest = tmp_path_factory.mktemp("bundles") / "bench_10k.tar.gz"
    bundle.export(store.kb_dir, dest=dest, actor="bench")
    return dest


def test_bundle_export_1k(benchmark, kb_1k, tmp_path):
    store = KBStore(kb_1k)
    dest = tmp_path / "out.tar.gz"
    benchmark(bundle.export, store.kb_dir, dest=dest, actor="bench")


def test_bundle_import_1k(benchmark, exported_bundle_1k, tmp_path):
    dest_kb = KBStore.init(tmp_path)
    benchmark(
        bundle.import_apply,
        dest_kb.kb_dir,
        exported_bundle_1k,
        on_conflict="overwrite",
        actor="bench",
    )


def test_bundle_export_check_1k(benchmark, exported_bundle_1k):
    benchmark(bundle.export_check, exported_bundle_1k)


def test_bundle_import_10k(benchmark, exported_bundle_10k, tmp_path):
    dest_kb = KBStore.init(tmp_path)
    benchmark(
        bundle.import_apply,
        dest_kb.kb_dir,
        exported_bundle_10k,
        on_conflict="overwrite",
        actor="bench",
    )
