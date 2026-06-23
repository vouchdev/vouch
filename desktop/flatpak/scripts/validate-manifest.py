#!/usr/bin/env python3
"""Validate flatpak packaging files (#211).

Exit 0 when no errors; 1 when validation fails. Warnings print but do not fail
unless --strict.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a script without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.validate import run_all_validations


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate vouch Flatpak packaging")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="desktop/flatpak directory",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as errors",
    )
    args = parser.parse_args()

    report = run_all_validations(args.root)
    for issue in report.issues:
        prefix = issue.level.upper()
        loc = f" ({issue.path})" if issue.path else ""
        print(f"{prefix}: {issue.message}{loc}")

    if report.errors:
        return 1
    if args.strict and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
