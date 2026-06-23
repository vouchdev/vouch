"""Free localhost port selection for the review-ui sidecar."""

from __future__ import annotations

import socket
from contextlib import closing


def pick_free_port(host: str = "127.0.0.1", start: int = 7780, attempts: int = 32) -> int:
    """Return the first bindable port in ``[start, start + attempts)``."""
    for port in range(start, start + attempts):
        if _port_available(host, port):
            return port
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _port_available(host: str, port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
