"""
IP-based rate limiting dependency for FastAPI routes.

Usage:
    @router.post("/endpoint", dependencies=[Depends(rate_limit("action", 5, 60))])
    def my_endpoint(): ...

This limits to 5 requests per 60 seconds per IP for "action".
"""

import ipaddress
import os

from fastapi import HTTPException, Request, status
from ..services.redis_service import check_rate_limit


# ── Trusted-proxy aware client-IP derivation ──────────────────────────────────
#
# The connecting peer (`request.client.host`) is the ONLY value we control end
# to end. Client-supplied forwarding headers (X-Real-Ip, X-Forwarded-For) are
# trivially spoofable, so honoring them blindly lets an attacker rotate the
# header on every request and defeat every per-IP limit (share-password
# brute-force, magic-code email-bomb, etc).
#
# Policy (identical in rate_limit.py and global_rate_limit.py — see
# `client_ip`, imported there):
#   * If the immediate peer is a TRUSTED proxy (Traefik / loopback / the
#     container network), trust the LAST hop of X-Forwarded-For (the address
#     the trusted proxy itself observed), falling back to X-Real-Ip.
#   * Otherwise ignore all forwarding headers and use the peer address.
#
# Trusted proxy CIDRs are configurable via the TRUSTED_PROXY_CIDRS env var
# (comma-separated). Default covers loopback + RFC1918 private ranges, which is
# where Traefik/the Docker network live in this deployment.
_DEFAULT_TRUSTED_PROXY_CIDRS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def _load_trusted_networks() -> list:
    raw = os.environ.get("TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED_PROXY_CIDRS)
    nets = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            # Skip malformed entries rather than crash request handling.
            continue
    return nets


_TRUSTED_NETWORKS = _load_trusted_networks()


def _is_trusted_proxy(peer: "str | None") -> bool:
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except (ValueError, TypeError):
        return False
    return any(addr in net for net in _TRUSTED_NETWORKS)


def client_ip(request: Request) -> str:
    """Derive the real client IP in a spoof-resistant way.

    Only honors X-Forwarded-For (last hop) / X-Real-Ip when the immediate peer
    is a known trusted proxy; otherwise returns the connecting peer address.
    """
    peer = request.client.host if request.client else None

    if _is_trusted_proxy(peer):
        # Prefer the last hop of X-Forwarded-For — the address the trusted
        # proxy actually saw. Earlier hops are client-controlled and untrusted.
        xff = request.headers.get("x-forwarded-for")
        if xff:
            last_hop = xff.split(",")[-1].strip()
            if last_hop:
                return last_hop
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

    return peer or "unknown"


def rate_limit(action: str, max_requests: int, window_seconds: int):
    """
    Returns a FastAPI dependency that enforces IP-based rate limiting.

    Args:
        action: Unique key for this rate limit (e.g. "send_magic_code")
        max_requests: Maximum requests allowed in the window
        window_seconds: Time window in seconds
    """

    def _dependency(request: Request):
        # Spoof-resistant client IP: forwarding headers are only honored when the
        # immediate peer is a known trusted proxy (see client_ip docstring).
        ip = client_ip(request)

        allowed, retry_after = check_rate_limit(ip, action, max_requests, window_seconds)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Please try again in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
