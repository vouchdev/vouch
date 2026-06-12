"""Enable ``python -m vouch``.

The console-script entry-point (`vouch = vouch.cli:cli`) is the canonical
surface, but the desktop AppImage launcher (and any environment that
shadows entry points) needs `python -m vouch` to work too. One-liner shim.
"""

from .cli import cli

if __name__ == "__main__":
    cli()
