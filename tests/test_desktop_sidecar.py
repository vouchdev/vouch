"""Integration test: spawn review-ui sidecar and poll healthz."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from vouch.desktop.sidecar import SidecarConfig, spawn_review_ui, terminate_sidecar, wait_for_health
from vouch.storage import KBStore

pytest.importorskip("fastapi", reason="sidecar test needs [web] extra")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_sidecar_healthz(tmp_path: Path) -> None:
    store = KBStore.init(tmp_path / "sidecar-proj")
    port = _free_port()
    handle = spawn_review_ui(
        SidecarConfig(project_root=str(store.root), port=port),
    )
    try:
        body = wait_for_health(handle, expected_root=str(store.root), timeout_s=45.0)
        assert body["ok"] is True
        assert body["kb_label"] == store.root.name
        assert body["pending"] == 0
    finally:
        terminate_sidecar(handle)
