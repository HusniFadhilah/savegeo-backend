"""Short-lived MapKit JS authorization tokens for the Apple satellite basemap.

Apple serves its imagery to web pages only through MapKit JS, which asks the
page for a JWT signed (ES256) with a MapKit key from the Apple Developer
portal. The private key never leaves the server; each token is bound to the
requesting SAVEGEO origin and expires quickly.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

import jwt

from app.core.config import Settings, get_settings


class AppleMapKitNotConfigured(RuntimeError):
    pass


def _private_key(settings: Settings) -> str:
    if settings.apple_mapkit_private_key.strip():
        return settings.apple_mapkit_private_key.strip().replace("\\n", "\n")
    if settings.apple_mapkit_private_key_file.strip():
        path = Path(settings.apple_mapkit_private_key_file.strip())
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AppleMapKitNotConfigured("Apple MapKit private key file is unreadable") from exc
    return ""


def is_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.apple_mapkit_team_id.strip()
        and settings.apple_mapkit_key_id.strip()
        and (settings.apple_mapkit_private_key.strip() or settings.apple_mapkit_private_key_file.strip())
    )


def token_origin(request_origin: str | None, referer: str | None = None, settings: Settings | None = None) -> str:
    """Bind tokens to the caller's origin only when it is an allowed SAVEGEO origin.

    Same-origin GET requests usually omit ``Origin``; the frontend's
    ``strict-origin`` referrer policy still sends the origin as ``Referer``.
    """
    settings = settings or get_settings()
    allowed = settings.allowed_origins_list
    candidates = [request_origin]
    if referer:
        try:
            parsed = urlsplit(referer)
            candidates.append(f"{parsed.scheme}://{parsed.netloc}")
        except ValueError:
            pass
    for candidate in candidates:
        if candidate and candidate in allowed:
            return candidate
    return settings.frontend_base_url.rstrip("/")


def create_token(origin: str, settings: Settings | None = None, now: float | None = None) -> tuple[str, int]:
    settings = settings or get_settings()
    if not is_configured(settings):
        raise AppleMapKitNotConfigured("Apple MapKit is not configured")
    key = _private_key(settings)
    if not key:
        raise AppleMapKitNotConfigured("Apple MapKit private key is empty")
    issued = int(now if now is not None else time.time())
    expires = issued + max(60, min(settings.apple_mapkit_token_ttl_seconds, 3600))
    token = jwt.encode(
        {"iss": settings.apple_mapkit_team_id.strip(), "iat": issued, "exp": expires, "origin": origin},
        key,
        algorithm="ES256",
        headers={"kid": settings.apple_mapkit_key_id.strip(), "typ": "JWT"},
    )
    return token, expires
