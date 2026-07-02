"""Enable ``python -m vouch`` as an alias for the ``vouch`` console script."""

from vouch.cli import cli

if __name__ == "__main__":
    cli()
