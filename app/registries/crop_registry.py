"""crop_registry.py — Static commodity + growth-stage catalog for Crop
Monitoring.

Pure metadata, no `ee`/DB/Flask dependency allowed here (mirrors
`vegetation_index_registry.py`'s separation of concerns). V1 growth-stage
inference is date-based only (days-since-planting -> stage lookup), no
NDVI-phenology curve fitting - see `crop_monitoring_service.growth_stage()`.

`stages` is an ascending list of {key, label, min_day, max_day} - `max_day`
of the last stage is `None` (open-ended, "still in/at this stage"). A day
count that falls before the first stage's `min_day` (i.e. negative -
monitoring date earlier than planting_date) is handled by the caller, not
here.
"""
from __future__ import annotations

from typing import Any

# Sawit (oil palm) is a perennial tree crop, not a single-season annual -
# its "stages" here describe the pre-productive establishment phase only
# (0 to ~30 months), after which it stays in a repeating ~10-14 day harvest
# cycle indefinitely rather than progressing through further stages. Flagged
# in its own `note` field rather than pretending it fits the annual-crop model.
CROP_CATALOG: dict[str, dict[str, Any]] = {
    "padi": {
        "label": "Padi",
        "category": "annual",
        "estimated_duration_days": 115,
        "stages": [
            {"key": "planting", "label": "Planting", "min_day": 0, "max_day": 0},
            {"key": "establishment", "label": "Establishment", "min_day": 1, "max_day": 15},
            {"key": "tillering", "label": "Tillering", "min_day": 16, "max_day": 40},
            {"key": "panicle_initiation", "label": "Panicle Initiation", "min_day": 41, "max_day": 60},
            {"key": "heading", "label": "Heading", "min_day": 61, "max_day": 75},
            {"key": "grain_filling", "label": "Grain Filling", "min_day": 76, "max_day": 100},
            {"key": "maturity", "label": "Maturity", "min_day": 101, "max_day": 115},
            {"key": "harvest", "label": "Harvest", "min_day": 116, "max_day": None},
        ],
    },
    "jagung": {
        "label": "Jagung",
        "category": "annual",
        "estimated_duration_days": 100,
        "stages": [
            {"key": "planting", "label": "Planting", "min_day": 0, "max_day": 0},
            {"key": "vegetative", "label": "Vegetative", "min_day": 1, "max_day": 30},
            {"key": "tasseling", "label": "Tasseling", "min_day": 31, "max_day": 50},
            {"key": "silking", "label": "Silking", "min_day": 51, "max_day": 60},
            {"key": "grain_filling", "label": "Grain Filling", "min_day": 61, "max_day": 90},
            {"key": "maturity", "label": "Maturity", "min_day": 91, "max_day": 100},
            {"key": "harvest", "label": "Harvest", "min_day": 101, "max_day": None},
        ],
    },
    "kedelai": {
        "label": "Kedelai",
        "category": "annual",
        "estimated_duration_days": 85,
        "stages": [
            {"key": "planting", "label": "Planting", "min_day": 0, "max_day": 0},
            {"key": "vegetative", "label": "Vegetative", "min_day": 1, "max_day": 25},
            {"key": "flowering", "label": "Flowering", "min_day": 26, "max_day": 40},
            {"key": "pod_filling", "label": "Pod Filling", "min_day": 41, "max_day": 65},
            {"key": "maturity", "label": "Maturity", "min_day": 66, "max_day": 85},
            {"key": "harvest", "label": "Harvest", "min_day": 86, "max_day": None},
        ],
    },
    "tebu": {
        "label": "Tebu",
        "category": "annual",
        "estimated_duration_days": 360,
        "stages": [
            {"key": "planting", "label": "Planting", "min_day": 0, "max_day": 0},
            {"key": "germination", "label": "Germination", "min_day": 1, "max_day": 40},
            {"key": "tillering", "label": "Tillering", "min_day": 41, "max_day": 120},
            {"key": "grand_growth", "label": "Grand Growth", "min_day": 121, "max_day": 270},
            {"key": "maturity", "label": "Maturity", "min_day": 271, "max_day": 360},
            {"key": "harvest", "label": "Harvest", "min_day": 361, "max_day": None},
        ],
    },
    "sawit": {
        "label": "Sawit",
        "category": "perennial",
        "estimated_duration_days": 900,  # ~30 months to first productive harvest
        "note": "Tanaman tahunan (perennial) - setelah fase 'Productive', panen TBS "
        "berulang setiap ~10-14 hari, bukan satu siklus panen tunggal seperti "
        "tanaman semusim.",
        "stages": [
            {"key": "planting", "label": "Planting", "min_day": 0, "max_day": 0},
            {"key": "nursery_transplant", "label": "Nursery/Transplant Establishment", "min_day": 1, "max_day": 365},
            {"key": "immature", "label": "Immature", "min_day": 366, "max_day": 899},
            {"key": "productive", "label": "Productive (recurring harvest cycle)", "min_day": 900, "max_day": None},
        ],
    },
}


def list_commodities() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": c["label"], "category": c["category"], "estimated_duration_days": c["estimated_duration_days"]}
        for key, c in CROP_CATALOG.items()
    ]


def get_commodity(key: str) -> dict[str, Any] | None:
    return CROP_CATALOG.get(key)


def get_catalog_payload() -> dict[str, Any]:
    return {"commodities": list_commodities()}


def estimate_harvest_days(commodity: str) -> int | None:
    crop = CROP_CATALOG.get(commodity)
    return crop["estimated_duration_days"] if crop else None


def resolve_growth_stage(commodity: str, days_since_planting: int) -> dict[str, Any] | None:
    """Date-based stage lookup (V1) - no NDVI-phenology inference. Returns
    None if `commodity` is unknown or `days_since_planting` is negative
    (monitoring date earlier than planting_date - caller's responsibility to
    treat that as "not yet planted", not a lookup failure)."""
    crop = CROP_CATALOG.get(commodity)
    if crop is None or days_since_planting < 0:
        return None
    for stage in crop["stages"]:
        if (stage["max_day"] is None or days_since_planting <= stage["max_day"]) and days_since_planting >= stage["min_day"]:
            return stage
    return crop["stages"][-1]
