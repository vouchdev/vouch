"""Tests for desktop port selection."""

from vouch.desktop.ports import _port_available, pick_free_port


def test_pick_free_port_returns_bindable_port() -> None:
    port = pick_free_port()
    assert _port_available("127.0.0.1", port)


def test_pick_free_port_skips_taken() -> None:
    import socket
    from contextlib import closing

    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        taken = int(sock.getsockname()[1])
        port = pick_free_port(start=taken, attempts=1)
        assert port != taken or _port_available("127.0.0.1", port)
