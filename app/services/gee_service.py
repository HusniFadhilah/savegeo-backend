"""Google Earth Engine initialization, ported from legacy `app.py::init_ee_from_db`.

Instead of an `EE_INITIALIZED` flag on Flask's global `app.config`, the result is
stored on `app.state.ee_initialized` (see app/main.py's lifespan) and read via the
`require_ee` FastAPI dependency (app/api/deps.py) on every GEE-gated route.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.gee_credential import GEECredential

logger = logging.getLogger(__name__)
_runtime_credential_id: int | None = None
_last_initialization_rate_limited = False


def _init_from_key_file(client_email: str, key_path: str, project_id: str | None = None) -> bool:
    import ee

    credentials = ee.ServiceAccountCredentials(client_email, key_path)
    # Supplying the configured Cloud project avoids relying on the default
    # Earth Engine discovery context, which can time out for service accounts.
    effective_project_id = project_id or get_settings().gee_project_id or None
    from app.core.pinned_provider_transport import BoundedEarthEngineTransport

    ee.Initialize(
        credentials,
        project=effective_project_id,
        http_transport=BoundedEarthEngineTransport(),
    )
    return True


def _init_from_key_data(client_email: str, key_bytes: bytes, project_id: str | None = None) -> bool:
    import ee

    credentials = ee.ServiceAccountCredentials(client_email, key_data=key_bytes.decode("utf-8"))
    effective_project_id = project_id or get_settings().gee_project_id or None
    from app.core.pinned_provider_transport import BoundedEarthEngineTransport

    ee.Initialize(
        credentials,
        project=effective_project_id,
        http_transport=BoundedEarthEngineTransport(),
    )
    return True


def _is_quota_or_rate_limit_error(exc: Exception) -> bool:
    """Do not switch Google identities to work around an Earth Engine quota."""
    if getattr(exc, "status_code", None) == 429:
        return True
    message = str(exc).lower()
    return any(token in message for token in (
        "429", "quota", "rate limit", "rate_limit", "too many requests",
        "too many concurrent aggregations", "resource_exhausted",
    ))


def _stored_credential_candidates(
    db: Session, exclude_credential_ids: set[int] | None = None,
) -> list[GEECredential]:
    """Return the primary credential followed by manually verified backups.

    Unchecked uploads are not eligible as runtime fallbacks. A credential whose
    latest probe failed is also skipped until an administrator probes it again.
    """
    excluded = exclude_credential_ids or set()
    primary = db.query(GEECredential).filter_by(is_active=True).first()
    candidates: list[GEECredential] = []
    if (primary is not None and primary.id not in excluded
            and getattr(primary, "last_check_status", None) != "failed"):
        candidates.append(primary)

    backups = db.query(GEECredential).filter(
        GEECredential.last_check_status == "ok",
    )
    if primary is not None:
        backups = backups.filter(GEECredential.id != primary.id)
    backups = backups.order_by(
        GEECredential.last_used_at.asc(), GEECredential.uploaded_at.asc(), GEECredential.id.asc(),
    )
    candidates.extend(row for row in backups.all() if row.id not in excluded)
    return candidates


def initialize_ee(db: Session, *, exclude_credential_ids: set[int] | None = None) -> bool:
    """Try the active key, then verified DB backups, then the configured env key.

    Earth Engine's Python client keeps authentication process-global, so this
    switches credentials only during initialization. It never rotates identities
    after a quota/rate-limit response; those failures must use bounded retry and
    the configured Earth Engine quotas.
    """
    global _runtime_credential_id, _last_initialization_rate_limited
    _runtime_credential_id = None
    _last_initialization_rate_limited = False
    for cred in _stored_credential_candidates(db, exclude_credential_ids):
        try:
            from app.services import storage_service

            key_bytes = storage_service.download_credential(cred.bucket_path)
            _init_from_key_data(cred.client_email, key_bytes, project_id=cred.project_id)
            cred.last_used_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            db.commit()
            _runtime_credential_id = cred.id
            logger.info("Earth Engine initialized from DB credential id=%s", cred.id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from DB credential failed: %s", type(exc).__name__)
            db.rollback()
            if _is_quota_or_rate_limit_error(exc):
                _last_initialization_rate_limited = True
                logger.warning("Earth Engine credential fallback stopped after a quota/rate-limit response")
                return False

    # --- Fallback: env vars ---
    settings = get_settings()
    key_file = settings.gee_key_file
    client_email = settings.gee_service_account

    if settings.gee_allow_legacy_plaintext_file_fallback and key_file and client_email and Path(key_file).exists():
        try:
            _init_from_key_file(client_email, key_file)
            _runtime_credential_id = None
            logger.info("Earth Engine initialized from the explicitly enabled local key-file fallback")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from env failed: %s", type(exc).__name__)
    elif key_file and not settings.gee_allow_legacy_plaintext_file_fallback:
        logger.warning("Plaintext Earth Engine key-file fallback is disabled; configure an encrypted DB credential")

    logger.warning("Earth Engine could not be initialized (no active DB credential or valid env fallback).")
    return False


def runtime_credential_id() -> int | None:
    """Credential selected in this process by its most recent initialization."""
    return _runtime_credential_id


def last_initialization_was_rate_limited() -> bool:
    return _last_initialization_rate_limited


def is_credential_auth_error(exc: Exception) -> bool:
    """True only for auth/permission failures, never for quota responses."""
    if _is_quota_or_rate_limit_error(exc):
        return False
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    if status != 403:
        return False
    message = str(exc).lower()
    return any(token in message for token in (
        "credential", "service account", "authentication", "unauthorized",
        "permission denied", "permission_denied", "forbidden", "access denied",
        "insufficient authentication scopes",
    ))


def get_active_credential(db: Session) -> GEECredential | None:
    return db.query(GEECredential).filter_by(is_active=True).first()
