"""Google Earth Engine initialization, ported from legacy `app.py::init_ee_from_db`.

Instead of an `EE_INITIALIZED` flag on Flask's global `app.config`, the result is
stored on `app.state.ee_initialized` (see app/main.py's lifespan) and read via the
`require_ee` FastAPI dependency (app/api/deps.py) on every GEE-gated route.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.gee_credential import GEECredential

logger = logging.getLogger(__name__)


def _init_from_key_file(client_email: str, key_path: str) -> bool:
    import ee

    credentials = ee.ServiceAccountCredentials(client_email, key_path)
    ee.Initialize(credentials)
    return True


def initialize_ee(db: Session) -> bool:
    """Try the active DB credential first (downloaded from Supabase Storage into a
    temp file for the duration of `ee.Initialize` only), then fall back to env vars.
    Returns whether GEE is now initialized.
    """
    cred = db.query(GEECredential).filter_by(is_active=True).first()

    if cred:
        try:
            from app.services import storage_service

            key_bytes = storage_service.download_credential(cred.bucket_path)
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp.write(key_bytes)
                tmp_path = tmp.name
            try:
                _init_from_key_file(cred.client_email, tmp_path)
                cred.last_used_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                db.commit()
                logger.info("Earth Engine initialized from DB credential: %s", cred.client_email)
                return True
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from DB credential failed: %s", exc)

    # --- Fallback: env vars ---
    settings = get_settings()
    key_file = settings.gee_key_file
    client_email = settings.gee_service_account

    if key_file and client_email and Path(key_file).exists():
        try:
            _init_from_key_file(client_email, key_file)
            logger.info("Earth Engine initialized from env: %s", client_email)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("GEE init from env failed: %s", exc)

    logger.warning("Earth Engine could not be initialized (no active DB credential or valid env fallback).")
    return False


def get_active_credential(db: Session) -> GEECredential | None:
    return db.query(GEECredential).filter_by(is_active=True).first()
