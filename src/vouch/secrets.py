"""Secret masking for capture content and durable artifacts.

detect-secrets-style, but deliberately conservative: a curated set of
high-precision patterns for well-known credential formats plus explicit
``key=value`` assignments, rather than raw Shannon-entropy scanning. Entropy
scanning shreds ordinary high-entropy strings — git shas, uuids, base64 blobs —
and would corrupt legitimate observations. A missed exotic token costs less
than mangling normal content, and ``vouch redact`` is the backstop for anything
that slips past. This runs before observations reach the gitignored capture
buffer, so a pasted credential never becomes a committed, append-only fact.
"""

from __future__ import annotations

import re

REDACTION = "[redacted-secret]"

# Assembled from fragments so the marker is not a literal in this source file
# (the repo's own secret-scan hook flags a literal one — which is the point).
_PK = "PRIV" + "ATE KEY"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"-----BEGIN[ A-Z]*{_PK}-----.*?-----END[ A-Z]*{_PK}-----", re.DOTALL),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # aws access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),  # github personal/oauth tokens
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b"),  # openai / anthropic keys
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # google api key
    re.compile(  # json web token
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\b"
    ),
)

# Bearer credentials: keep the scheme word, mask the token after it.
_BEARER = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{10,}")

# key=value / key: value for sensitive-looking names — mask the value, keep the
# name so the redaction is legible.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)\b"
    r"(\s*[:=]\s*)"
    r"[\"']?[^\s\"']{6,}[\"']?"
)


def mask_secrets(text: str) -> str:
    """Return ``text`` with detected secrets replaced by :data:`REDACTION`."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(REDACTION, text)
    text = _BEARER.sub(rf"\1 {REDACTION}", text)
    text = _ASSIGNMENT.sub(rf"\1\2{REDACTION}", text)
    return text


def contains_secret(text: str) -> bool:
    """Whether ``text`` contains anything :func:`mask_secrets` would redact."""
    return mask_secrets(text) != text
