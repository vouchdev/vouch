"""Spawn and terminate the ``vouch review-ui`` sidecar process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 7780
STARTUP_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.15
TERMINATE_TIMEOUT_S = 5.0


@dataclass
class SidecarConfig:
    project_root: str
    host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_BIND_PORT
    reviewer: str = "desktop-reviewer"
    vouch_executable: str | None = None


@dataclass
class SidecarHandle:
    process: subprocess.Popen[Any]
    config: SidecarConfig
    base_url: str

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/healthz"


def _vouch_cmd(config: SidecarConfig) -> list[str]:
    exe = config.vouch_executable
    if exe:
        return [exe]
    return [sys.executable, "-m", "vouch.cli"]


def spawn_review_ui(config: SidecarConfig) -> SidecarHandle:
    """Start ``vouch review-ui`` against ``config.project_root``."""
    bind = f"{config.host}:{config.port}"
    cmd = [
        *_vouch_cmd(config),
        "review-ui",
        "--bind",
        bind,
        "--kb",
        config.project_root,
        "--no-open-browser",
        "--reviewer",
        config.reviewer,
    ]
    env = os.environ.copy()
    # Child should not inherit a forced KB path from the parent shell.
    env.pop("VOUCH_KB_PATH", None)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    base_url = f"http://{config.host}:{config.port}"
    return SidecarHandle(process=proc, config=config, base_url=base_url)


def wait_for_health(
    handle: SidecarHandle,
    *,
    timeout_s: float = STARTUP_TIMEOUT_S,
    expected_root: str | None = None,
) -> dict[str, Any]:
    """Poll ``/healthz`` until the sidecar is ready or timeout."""
    deadline = time.monotonic() + timeout_s
    last_error = "sidecar did not become healthy in time"
    while time.monotonic() < deadline:
        if handle.process.poll() is not None:
            out = ""
            if handle.process.stdout is not None:
                out = handle.process.stdout.read() or ""
            raise RuntimeError(
                f"review-ui exited with code {handle.process.returncode}: {out[:500]}"
            )
        try:
            with urlopen(handle.health_url, timeout=1.0) as resp:
                body = resp.read().decode("utf-8")
        except (URLError, TimeoutError, OSError) as e:
            last_error = str(e)
            time.sleep(POLL_INTERVAL_S)
            continue
        import json

        data = json.loads(body)
        if not data.get("ok"):
            last_error = "healthz returned ok=false"
            time.sleep(POLL_INTERVAL_S)
            continue
        if expected_root is not None:
            kb = str(data.get("kb", ""))
            if Path(kb).resolve() != Path(expected_root).resolve():
                last_error = f"healthz kb mismatch: {kb!r} != {expected_root!r}"
                time.sleep(POLL_INTERVAL_S)
                continue
        return data
    raise TimeoutError(last_error)


def terminate_sidecar(
    handle: SidecarHandle,
    *,
    timeout_s: float = TERMINATE_TIMEOUT_S,
) -> None:
    """Gracefully stop a running sidecar."""
    proc = handle.process
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.0)
