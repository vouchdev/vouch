"""Snapshot a URL into the content-addressed source store.

Evidence intake for web content: fetch the exact bytes once, register them
via `KBStore.put_source` (sha256 content addressing, deliberately below the
review gate like every other source), and let claims cite the immutable
snapshot id. The live page can drift; the evidence a reviewer approved
against cannot.

Conservative by default, because this is the first outbound network call in
the intake path:

- http/https only; every hop of a redirect chain is re-validated
- hosts must resolve to public addresses (loopback / private / link-local /
  reserved ranges are refused), so a snapshot can't be pointed at the
  operator's own network. resolution and connection are separate steps, so
  a hostile DNS server could still rebind between them — the guard is a
  seatbelt against lazy SSRF, not a substitute for network policy.
- bodies are capped (default 2 MiB) and stored as raw bytes; no decoding is
  attempted here, so charset quirks stay a consumer concern.

Never imports proposals or lifecycle: registering a source writes evidence,
not knowledge.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import Source
from .storage import KBStore

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 5

_USER_AGENT = "vouch-source-fetch"


class FetchError(ValueError):
    """Raised when a URL cannot be snapshotted safely."""


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    final_url: str
    media_type: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _addr_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check_url(url: str, *, allow_private: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"only http/https URLs can be snapshotted, got {url!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"URL has no host: {url!r}")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"cannot resolve {host!r}: {e}") from e
    for info in infos:
        address = str(info[4][0])
        if not _addr_is_public(address):
            raise FetchError(
                f"{host!r} resolves to non-public address {address} — refusing to fetch"
            )


def fetch_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: bool = False,
) -> FetchResult:
    """Fetch `url` with redirect re-validation and a byte cap.

    allow_private skips the public-address check — used by tests that run a
    loopback fixture server; production callers leave it False.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(current, allow_private=allow_private)
        req = urllib.request.Request(current, headers={"User-Agent": _USER_AGENT})
        try:
            resp = _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                target = e.headers.get("Location")
                e.close()
                if not target:
                    raise FetchError(f"redirect from {current!r} without Location") from e
                current = urllib.parse.urljoin(current, target)
                continue
            raise FetchError(f"HTTP {e.code} fetching {current!r}") from e
        except OSError as e:
            raise FetchError(f"fetch failed for {current!r}: {e}") from e
        with resp:
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError(
                    f"{current!r} exceeds the {max_bytes}-byte snapshot cap"
                )
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            media_type = content_type.split(";")[0].strip() or "application/octet-stream"
            return FetchResult(content=body, final_url=current, media_type=media_type)
    raise FetchError(f"too many redirects fetching {url!r}")


def snapshot_url(
    store: KBStore,
    url: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
    allow_private: bool = False,
) -> Source:
    """Fetch `url` and register the exact bytes as a content-addressed Source."""
    result = fetch_url(
        url, max_bytes=max_bytes, timeout=timeout, allow_private=allow_private,
    )
    return store.put_source(
        result.content,
        title=title or url,
        url=result.final_url,
        locator=url,
        source_type="url",
        media_type=result.media_type,
        tags=tags,
        metadata={
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "final_url": result.final_url,
        },
    )
