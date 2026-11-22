"""Resolve forwarding headers only when the immediate peer is trusted."""

from ipaddress import ip_address, ip_network
from starlette.requests import Request

from app.core.config import Settings, get_settings


def get_client_ip(request: Request, settings: Settings | None = None) -> str:
    peer = request.client.host if request.client else "unknown"
    config = settings or get_settings()
    networks = [ip_network(value.strip(), strict=False) for value in config.trusted_proxy_cidrs.split(",") if value.strip()]
    def trusted(value: str) -> bool:
        try:
            return any(ip_address(value) in network for network in networks)
        except ValueError:
            return False
    if not trusted(peer):
        return peer
    chain = request.headers.get("x-forwarded-for", "").split(",")
    if len(chain) > 32:
        return peer
    for value in reversed(chain):
        candidate = value.strip()
        try:
            ip_address(candidate)
        except ValueError:
            return peer
        if not trusted(candidate):
            return candidate
    return peer
