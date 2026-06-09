"""pytest-benchmark configuration for vouch benchmarks."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def pytest_configure(config):
    """Require pytest-benchmark."""
    try:
        import pytest_benchmark  # noqa: F401
    except ImportError:
        pytest.exit(
            "pytest-benchmark is required: pip install pytest-benchmark",
            returncode=1,
        )


@pytest.fixture(scope="session")
def kb_1k(tmp_path_factory):
    return _gen_kb(tmp_path_factory, claims=1_000)


@pytest.fixture(scope="session")
def kb_10k(tmp_path_factory):
    return _gen_kb(tmp_path_factory, claims=10_000)


@pytest.fixture(scope="session")
def kb_100k(tmp_path_factory):
    return _gen_kb(tmp_path_factory, claims=100_000)


def _gen_kb(tmp_path_factory, *, claims: int) -> Path:
    out = tmp_path_factory.mktemp(f"kb{claims}")
    subprocess.check_call(
        [sys.executable, "benchmarks/fixtures/gen_kb.py",
         "--out", str(out), "--claims", str(claims)],
    )
    return out
