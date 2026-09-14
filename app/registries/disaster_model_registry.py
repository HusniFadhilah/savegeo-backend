"""Available disaster methods and existing hosted Dynamic World predictions.

Dynamic World supplies semantic land-cover classes, not disaster damage labels.
Disabled object models have no runnable implementation.
"""
from __future__ import annotations

from app.registries.landcover_dataset_registry import LAND_COVER_LEGENDS

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


DISASTER_MODEL_REGISTRY["dynamic_world_v1"] = {
    "model_id": "dynamic_world_v1", "backend_label": "Dynamic World V1",
    "user_label": "Segmentasi tutupan lahan pre/post", "category": "segmentation",
    "version": "1.0", "enabled": True, "input_type": ["pre_imagery", "post_imagery"],
    "output_type": "raster+statistics+confidence", "satellite": "Sentinel-2",
    "resolution_m": 10, "temporal_mode": "independent_published_inference",
    "dataset": "GOOGLE/DYNAMICWORLD/V1", "multiclass": True,
    "description": "Prediksi model Dynamic World yang sudah tersedia, dipilih terpisah pada tanggal pre/post. Sembilan kelas tutupan lahan; bukan diagnosis kerusakan atau model change-detection langsung.",
    "reliability": "Probabilitas kelas tersedia; akurasi dampak bencana belum divalidasi.",
    "disaster_types": ["flood", "landslide", "forest_fire", "tsunami", "earthquake", "volcanic_eruption", "storm", "drought", "other"],
    "classes": [{"class_id": int(key), **value} for key, value in LAND_COVER_LEGENDS["Dynamic_World"].items()],
}
for _key, _types in {
    "flood_change_v1": ["flood", "tsunami"],
    "water_segmentation_v1": ["flood", "tsunami"],
    "forest_change_v1": ["forest_fire", "landslide", "drought", "storm", "other"],
}.items():
    DISASTER_MODEL_REGISTRY[_key].update(disaster_types=_types, multiclass=False,
        reliability="Metode indeks/ambang; bukan model kerusakan terlatih.")


def list_models(enabled_only: bool = False, disaster_type: str | None = None) -> list[dict]:
    values = list(DISASTER_MODEL_REGISTRY.values())
    if enabled_only:
        values = [m for m in values if m["enabled"]]
    if disaster_type:
        values = [m for m in values if disaster_type in m.get("disaster_types", [])]
    return sorted(values, key=lambda m: (bool(m.get("multiclass")), tuple(int(n) for n in m["version"].split("."))), reverse=True)


def get_model(model_id: str) -> dict | None:
    return DISASTER_MODEL_REGISTRY.get(model_id)


def is_enabled(model_id: str) -> bool:
    model = DISASTER_MODEL_REGISTRY.get(model_id)
    return bool(model and model["enabled"])
