"""Static analysis-model catalog for the Disaster Intelligence Dashboard
(spec sections 6-7, 54, 57). Same "static registry, admin can enable/disable
via curated dict" convention as `carbon_dataset_registry.py` /
`landcover_dataset_registry.py` - adding a model later means adding one entry
here plus (if it's a real, runnable model) a compute function in
`app/services/disaster_analysis_service.py`. No DB row needed to add a model.

Only 3 entries are `enabled: True` today - the rest require an object-detection
/ change-detection model that has not been trained anywhere in this codebase
(verified: zero hits for road-damage/building-change/building-segmentation
across both `backend/` legacy and `savegeo/backend/`). Registering them
disabled keeps the architecture future-proof (spec section 57: add a model
without redesign) without ever faking a result (spec section 55-56).

`user_label` is what User-facing UI must render (spec section 54 - "Run Flood
Detection" internally is surfaced to User as "Flood Change", never the raw
model_id). `backend_label` is the internal/versioned name, shown only in Admin
UI and Analysis Information metadata (spec section 34).
"""
from __future__ import annotations

DISASTER_MODEL_REGISTRY: dict[str, dict] = {
    "flood_change_v1": {
        "model_id": "flood_change_v1",
        "backend_label": "flood_change_v1",
        "user_label": "Flood Change",
        "category": "change_detection",
        "version": "1.0",
        "input_type": ["pre_imagery", "post_imagery"],
        "output_type": "raster+polygon",
        "satellite": "Sentinel-1 SAR GRD",
        "description": "Perbandingan citra SAR sebelum/sesudah untuk mendeteksi area tergenang baru.",
        "enabled": True,
    },
    "water_segmentation_v1": {
        "model_id": "water_segmentation_v1",
        "backend_label": "water_segmentation_v1",
        "user_label": "Water Extent",
        "category": "segmentation",
        "version": "1.0",
        "input_type": ["pre_imagery", "post_imagery"],
        "output_type": "raster",
        "satellite": "Sentinel-2 Optical (NDWI)",
        "description": "Segmentasi tutupan air permukaan berbasis indeks NDWI pada satu tanggal citra.",
        "enabled": True,
    },
    "forest_change_v1": {
        "model_id": "forest_change_v1",
        "backend_label": "forest_change_v1",
        "user_label": "Forest Cover Change",
        "category": "change_detection",
        "version": "1.0",
        "input_type": ["pre_imagery", "post_imagery"],
        "output_type": "raster+polygon",
        "satellite": "Sentinel-2 Optical (NDVI)",
        "description": "Perbandingan indeks NDVI sebelum/sesudah untuk mengindikasikan perubahan tutupan hutan.",
        "enabled": True,
    },
    "building_change_v1": {
        "model_id": "building_change_v1",
        "backend_label": "building_change_v1",
        "user_label": "Building Change",
        "category": "change_detection",
        "version": "0.0",
        "input_type": ["pre_imagery", "post_imagery"],
        "output_type": "polygon",
        "satellite": None,
        "description": "Belum tersedia - memerlukan model deteksi perubahan bangunan yang belum dilatih.",
        "enabled": False,
    },
    "building_segmentation_v1": {
        "model_id": "building_segmentation_v1",
        "backend_label": "building_segmentation_v1",
        "user_label": "Building Inventory",
        "category": "segmentation",
        "version": "0.0",
        "input_type": ["post_imagery"],
        "output_type": "polygon",
        "satellite": None,
        "description": "Belum tersedia - memerlukan model segmentasi objek bangunan yang belum dilatih.",
        "enabled": False,
    },
    "road_damage_v1": {
        "model_id": "road_damage_v1",
        "backend_label": "road_damage_v1",
        "user_label": "Road Impact",
        "category": "damage_assessment",
        "version": "0.0",
        "input_type": ["pre_imagery", "post_imagery"],
        "output_type": "polygon",
        "satellite": None,
        "description": "Belum tersedia - memerlukan model deteksi kerusakan jalan yang belum dilatih.",
        "enabled": False,
    },
}


def list_models(enabled_only: bool = False) -> list[dict]:
    values = list(DISASTER_MODEL_REGISTRY.values())
    if enabled_only:
        values = [m for m in values if m["enabled"]]
    return values


def get_model(model_id: str) -> dict | None:
    return DISASTER_MODEL_REGISTRY.get(model_id)


def is_enabled(model_id: str) -> bool:
    model = DISASTER_MODEL_REGISTRY.get(model_id)
    return bool(model and model["enabled"])
