"""Year/model/AOI-specific summaries that survive the map-ID cache lifetime."""
import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, date, datetime

from app.db.models.carbon_annual_estimate import CarbonAnnualEstimate

logger = logging.getLogger(__name__)

# Bump when carbon_pool_service._landsat_predictors changes (QA mask bits, collection
# choice, compositing); otherwise summaries from the old algorithm would be reused.
ANNUAL_ALGORITHM_VERSION = "landsat-annual-median-v1"


def estimate_identity(data, model, analysis_year, reference_year, scale, today=None):
    today = today or datetime.now(UTC).date()
    # Historical years are complete; the current year must acquire new imagery.
    observed_through = min(date(analysis_year, 12, 31), today)
    aoi_json = json.dumps(data["aoi"], sort_keys=True, separators=(",", ":"))
    aoi_checksum = hashlib.sha256(aoi_json.encode()).hexdigest()
    identity = {
        "schema": 1, "year": analysis_year, "reference_year": reference_year,
        "observed_through": observed_through.isoformat(), "aoi": aoi_checksum,
        "dataset": data["reference_dataset"], "model": model["name"],
        "version": model.get("version"), "metadata": model["metadata_json"],
        "scale": scale, "clipped": bool(data.get("clip_to_aoi", True)),
        "algorithm": ANNUAL_ALGORITHM_VERSION,
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return key, aoi_checksum, observed_through


def load_summary(db, key):
    try:
        row = db.get(CarbonAnnualEstimate, key)
        return deepcopy(row.summary) if row else None
    except Exception:  # noqa: BLE001 - storage must not prevent a fresh analysis
        db.rollback()
        logger.warning("Annual carbon summary read failed", exc_info=True)
        return None


def save_summary(db, identity, data, model, analysis_year, reference_year, summary):
    key, aoi_checksum, observed_through = identity
    try:
        if db.get(CarbonAnnualEstimate, key) is None:
            db.add(CarbonAnnualEstimate(
                cache_key=key, analysis_year=analysis_year, reference_year=reference_year,
                dataset_key=data["reference_dataset"], model_name=model["name"],
                aoi_checksum=aoi_checksum, observed_through=observed_through,
                summary=deepcopy(summary),
            ))
            db.commit()
        return True
    except Exception:  # noqa: BLE001 - include concurrent writers; keep successful result
        db.rollback()
        logger.warning("Annual carbon summary write failed", exc_info=True)
        return False
