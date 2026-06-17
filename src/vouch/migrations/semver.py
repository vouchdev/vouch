"""A deliberately tiny semver subset (``MAJOR.MINOR.PATCH``, no pre-release tags).

Schema versions are short and fully controlled by manifests in-repo, so a 30-line
parser beats a dependency. Parsed versions are plain int tuples and compare with
the usual operators.
"""

from __future__ import annotations

import re

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


def parse(value: str) -> Version:
    m = _SEMVER_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid schema version {value!r} (expected MAJOR.MINOR.PATCH)")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_valid(value: str) -> bool:
    return bool(_SEMVER_RE.match(value.strip()))


def lt(a: str, b: str) -> bool:
    return parse(a) < parse(b)


def le(a: str, b: str) -> bool:
    return parse(a) <= parse(b)


def eq(a: str, b: str) -> bool:
    return parse(a) == parse(b)
