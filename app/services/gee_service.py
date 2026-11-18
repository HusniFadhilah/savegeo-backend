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


def _init_from_key_file(client_email: str, key_path: str, project_id: str | None = None) -> bool:
    import ee

    credentials = ee.ServiceAccountCredentials(client_email, key_path)
    # Supplying the configured Cloud project avoids relying on the default
    # Earth Engine discovery context, which can time out for service accounts.
    effective_project_id = project_id or get_settings().gee_project_id or None
    ee.Initialize(credentials, project=effective_project_id)
    return True


def _init_from_key_data(client_email: str, key_bytes: bytes, project_id: str | None = None) -> bool:
    import ee

    credentials = ee.ServiceAccountCredentials(client_email, key_data=key_bytes.decode("utf-8"))
    effective_project_id = project_id or get_settings().gee_project_id or None
    ee.Initialize(credentials, project=effective_project_id)
    return True


def initialize_ee(db: Session) -> bool:
    """Try the active DB credential first (decrypted in process memory), then env.
    Returns whether GEE is now initialized.
    """
    cred = db.query(GEECredential).filter_by(is_active=True).first()

    if cred:
        try:
            from app.services import storage_service

            key_bytes = storage_service.download_credential(cred.bucket_path)
            _init_from_key_data(cred.client_email, key_bytes, project_id=cred.project_id)
            cred.last_used_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            db.commit()
            logger.info("Earth Engine initialized from DB credential id=%s", cred.id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from DB credential failed: %s", type(exc).__name__)

    # --- Fallback: env vars ---
    settings = get_settings()
    key_file = settings.gee_key_file
    client_email = settings.gee_service_account

    if settings.gee_allow_legacy_plaintext_file_fallback and key_file and client_email and Path(key_file).exists():
        try:
            _init_from_key_file(client_email, key_file)
            logger.info("Earth Engine initialized from the explicitly enabled local key-file fallback")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from env failed: %s", type(exc).__name__)
    elif key_file and not settings.gee_allow_legacy_plaintext_file_fallback:
        logger.warning("Plaintext Earth Engine key-file fallback is disabled; configure an encrypted DB credential")

    logger.warning("Earth Engine could not be initialized (no active DB credential or valid env fallback).")
    return False


def get_active_credential(db: Session) -> GEECredential | None:
    return db.query(GEECredential).filter_by(is_active=True).first()
