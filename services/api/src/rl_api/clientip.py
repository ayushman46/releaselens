"""The client's IP address, aware of trusted reverse proxies.

``X-Forwarded-For`` is attacker-controlled unless it was written by a proxy we run. The
address is therefore resolved like this:

1. Start from the TCP peer (``request.client``). If it is not in ``RL_TRUSTED_PROXIES``,
   it *is* the client; any ``X-Forwarded-For`` is ignored.
2. Otherwise walk ``X-Forwarded-For`` from the right (the hop our proxy appended) and
   return the first address that is not itself a trusted proxy.

Rate limits and the audit log both use this value, so a client cannot dodge a limit or
forge its audit IP by sending the header itself. Values that are not IP addresses (the
Starlette test client reports ``"testclient"``) resolve to ``None``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

from starlette.requests import Request

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse(value: str | None) -> IPAddress | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(ip: IPAddress, networks: Sequence[IPNetwork]) -> bool:
    return any(ip in net for net in networks)


def resolve_client_ip(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[IPNetwork]
) -> str | None:
    peer_ip = _parse(peer)
    if peer_ip is None:
        return None
    if not trusted or not _trusted(peer_ip, trusted):
        return str(peer_ip)
    hops = [h.strip() for h in (forwarded_for or "").split(",") if h.strip()]
    for hop in reversed(hops):
        ip = _parse(hop)
        if ip is None:
            # A garbled hop: everything left of it is unverifiable. The last proxy we trust
            # is the best address we know.
            return str(peer_ip)
        if not _trusted(ip, trusted):
            return str(ip)
    return str(_parse(hops[0])) if hops else str(peer_ip)


def client_ip(request: Request) -> str | None:
    """Resolved once per request and cached on ``request.state``."""
    cached: str | None
    if hasattr(request.state, "client_ip"):
        cached = request.state.client_ip
        return cached
    trusted: Sequence[IPNetwork] = getattr(request.app.state, "trusted_proxies", ())
    resolved = resolve_client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trusted,
    )
    request.state.client_ip = resolved
    return resolved


def user_agent(request: Request, limit: int = 512) -> str | None:
    value = request.headers.get("user-agent")
    return value[:limit] if value else None
