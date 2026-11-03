import hashlib
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from app.db.models.crop_monitoring_cache import CropMonitoringCache

logger = logging.getLogger(__name__)
TTL_SECONDS = 1800  # Below the temporary GEE Map ID lifetime.


def section_key(field, parameters, section):
    common = ["start_date", "end_date", "scale", "satellite", "cloud_threshold", "cloud_mask_technique"]
    keys = {
        "landcover": ["start_date", "end_date", "scale"],
        "growth_stage": [],
        "weather_inputs": ["start_date", "end_date", "weather_source"],
        "weather": ["start_date", "end_date", "weather_source"],
        "water_moisture": common + ["weather_source"],
        "timeseries": common + ["timeseries_index"],
        "anomaly": common + ["historical_years"],
        "productivity_zones": common + ["productivity_zone_periods", "data_sources"],
        "historical_comparison": common + ["compare_years"],
        "flood": ["flood"],
    }.get(section, common)
    identity = {"schema": 1, "field": field.to_dict(include_geojson=True),
                "parameters": {key: parameters.get(key) for key in keys}, "section": section,
                "today": datetime.now(UTC).date().isoformat()}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def load(db, key):
    try:
        row = db.get(CropMonitoringCache, key)
        if row:
            expires = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
            if expires > datetime.now(UTC):
                return deepcopy(row.result)
    except Exception:  # noqa: BLE001 - cache unavailable means a fresh computation
        db.rollback()
        logger.warning("Crop section cache read failed", exc_info=True)
    return None


def save(db, key, field_id, section, result):
    try:
        row = db.get(CropMonitoringCache, key)
        if row is None:
            row = CropMonitoringCache(cache_key=key, field_id=field_id, section=section)
            db.add(row)
        row.result = deepcopy(result)
        row.expires_at = datetime.now(UTC) + timedelta(seconds=TTL_SECONDS)
        db.commit()
    except Exception:  # noqa: BLE001 - successful analyses survive concurrent cache writes
        db.rollback()
        logger.warning("Crop section cache write failed", exc_info=True)
