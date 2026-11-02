"""SSRF-hardened image fetcher (Person 2 — AI subsystem).

Blocks all RFC 1918 private ranges, loopback, link-local, and ULA IPv6.
Only allows HTTPS on port 443. Content-type allow-list enforced.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import FrozenSet

# ---------------------------------------------------------------------------
# Allow-listed MIME types for images
# ---------------------------------------------------------------------------
_ALLOWED_CONTENT_TYPES: FrozenSet[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/bmp",
        "image/tiff",
    }
)

# ---------------------------------------------------------------------------
# Blocked private / reserved IP networks (RFC 1918, loopback, link-local, ULA)
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # shared address space
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6
    ipaddress.ip_network("::1/128"),          # loopback
    ipaddress.ip_network("fc00::/7"),          # ULA
    ipaddress.ip_network("fe80::/10"),         # link-local
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::ffff:0:0/96"),     # IPv4-mapped
]


# Hostnames that are always blocked regardless of DNS
_BLOCKED_HOSTNAMES: FrozenSet[str] = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "broadcasthost",
        "metadata.google.internal",
        "169.254.169.254",  # AWS/GCP IMDS
    }
)


def is_blocked_host(host: str) -> bool:
    """Return True if *host* resolves to a blocked (private/reserved) address.

    Checks (in order):
    1. Known-blocked hostnames (e.g. localhost).
    2. If host is a literal IP address, check against blocked networks.
    3. DNS resolution — if any resolved address is private, block it.
       On DNS failure, returns False (let higher-level guards handle it).
    """
    host_lower = host.strip().lower()

    # Always block known hostnames
    if host_lower in _BLOCKED_HOSTNAMES:
        return True

    # Try as a literal IP address first
    try:
        addr = ipaddress.ip_address(host)
        return _is_blocked_addr(addr)
    except ValueError:
        pass  # host is a DNS name, resolve it

    # DNS resolution
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        for _, _, _, _, sockaddr in info:
            addr = ipaddress.ip_address(sockaddr[0])
            if _is_blocked_addr(addr):
                return True
        return False
    except OSError:
        # DNS failure → cannot confirm it's private; treat as NOT blocked here.
        # The actual HTTP connection will fail naturally if the host is unreachable.
        return False


def _is_blocked_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            return True
    return False


def is_blocked_url(url: str) -> bool:
    """Return True if the URL targets a blocked host or uses a non-HTTPS scheme.

    Blocked conditions:
    - Non-HTTPS scheme (must be exactly 'https')
    - Non-standard port (must be 443 or absent)
    - URL contains credentials (user:pass@host)
    - Host resolves to a private/reserved address
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return True

    if parsed.scheme != "https":
        return True

    # Reject URLs containing embedded credentials
    if parsed.username or parsed.password:
        return True

    port = parsed.port
    if port is not None and port != 443:
        return True

    host = parsed.hostname or ""
    if not host:
        return True

    return is_blocked_host(host)


def content_type_allowed(content_type: str) -> bool:
    """Return True if the Content-Type is in the image allow-list."""
    mime = content_type.split(";")[0].strip().lower()
    return mime in _ALLOWED_CONTENT_TYPES


async def safe_fetch_image(url: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
    """Fetch an image from *url* with SSRF and content-type guards.

    Raises:
        ValueError: if the URL or resolved host is blocked.
        ValueError: if the Content-Type is not an allowed image type.
        ValueError: if the response body exceeds *max_bytes*.
    """
    if is_blocked_url(url):
        raise ValueError(f"SSRF guard: URL blocked — {url!r}")

    # Lazy import so that the module loads even without aiohttp installed.
    try:
        import aiohttp  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "aiohttp is required for safe_fetch_image. "
            "Install it with: pip install aiohttp"
        ) from exc

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if not content_type_allowed(ct):
                raise ValueError(
                    f"SSRF guard: Content-Type not allowed — {ct!r}"
                )
            data = await resp.read()
            if len(data) > max_bytes:
                raise ValueError(
                    f"SSRF guard: response body too large "
                    f"({len(data)} > {max_bytes} bytes)"
                )
            return data
