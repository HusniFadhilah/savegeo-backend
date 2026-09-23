"""Capability contract for disaster analysis methods.

The registry describes what a method means and which inputs it accepts.  It is
deliberately explicit about indicator-only methods: Dynamic World, NDVI, NDWI
and SAR change are not damage labels.
"""
from __future__ import annotations

from app.registries.landcover_dataset_registry import LAND_COVER_LEGENDS

_ALL_TYPES = ["flood", "landslide", "forest_fire", "earthquake", "tsunami", "volcanic_eruption", "storm", "drought"]


def _method(model_id: str, *, disaster_types: list[str], category: str, result_semantics: str,
            description: str, enabled: bool, version: str = "1.0", damage_model: bool = False,
            validation_status: str = "method_validation_only", accepted_source_kind: list[str] | None = None,
            accepted_sensors: list[str] | None = None, required_bands: list[str] | None = None,
            requires_pre: bool = True, requires_post: bool = True, output_type: str = "raster+statistics",
            resolution_m: float | None = None, limitations: list[str] | None = None,
            availability_reason: str | None = None, **extra) -> dict:
    source_kind = accepted_source_kind or ["gee"]
    return {
        "model_id": model_id, "backend_label": model_id,
        "user_label": extra.pop("user_label", model_id), "version": version,
        "disaster_types": disaster_types, "category": category,
        "result_semantics": result_semantics, "damage_model": damage_model,
        "validation_status": validation_status, "required_sources": source_kind,
        "accepted_source_kind": source_kind, "accepted_sensors": accepted_sensors or [],
        "required_bands": required_bands or [], "supports_local_upload": "local_upload" in source_kind,
        "requires_pre": requires_pre, "requires_post": requires_post, "output_type": output_type,
        "resolution_m": resolution_m, "data_dependencies": list(extra.pop("data_dependencies", [])),
        "publication_requirements": list(extra.pop("publication_requirements", [
            "valid_tile_or_output", "valid_statistics", "complete_provenance", "quality_check_passed",
        ])), "limitations": limitations or [], "availability_reason": availability_reason,
        "enabled": enabled,
        "input_type": [p for p, required in (("pre_imagery", requires_pre), ("post_imagery", requires_post)) if required],
        "satellite": extra.pop("satellite", None),
        "requires_earth_engine": extra.pop("requires_earth_engine", True),
        "description": description, **extra,
    }


DISASTER_MODEL_REGISTRY: dict[str, dict] = {
    "flood_change_v1": _method(
        "flood_change_v1", disaster_types=["flood"], category="change_detection",
        result_semantics="inundation_change_indicator", description="Perbandingan Sentinel-1 SAR pre/post untuk indikator perubahan genangan.",
        enabled=True, accepted_sensors=["sentinel1", "sentinel-1"], required_bands=["VV"], output_type="raster+polygon",
        satellite="Sentinel-1 SAR GRD", limitations=["Indikator perubahan genangan, bukan estimasi kerusakan terlatih."],
        user_label="Flood Change", data_dependencies=["COPERNICUS/S1_GRD"],
    ),
    "water_segmentation_v1": _method(
        "water_segmentation_v1", disaster_types=["flood"], category="segmentation", result_semantics="water_extent",
        description="Luas air berbasis NDWI Sentinel-2 pre/post.", enabled=True,
        accepted_sensors=["sentinel2", "sentinel-2"], required_bands=["B3", "B8"], output_type="raster",
        satellite="Sentinel-2 Optical (NDWI)", limitations=["Luas air bukan kelas kerusakan bencana; bedakan luas air total dan perubahannya."],
        user_label="Water Extent", data_dependencies=["COPERNICUS/S2_SR_HARMONIZED"],
    ),
    "forest_change_v1": _method(
        "forest_change_v1", disaster_types=["forest_fire", "storm"], category="change_detection",
        result_semantics="vegetation_change_indicator", description="Perbandingan NDVI Sentinel-2 untuk indikator perubahan tutupan vegetasi.",
        enabled=True, accepted_sensors=["sentinel2", "sentinel-2"], required_bands=["B4", "B8"], output_type="raster+polygon",
        satellite="Sentinel-2 Optical (NDVI)", limitations=["Perubahan NDVI bukan otomatis luas terbakar, longsor, atau kerusakan."],
        user_label="Forest Cover Change", data_dependencies=["COPERNICUS/S2_SR_HARMONIZED"],
    ),
    "dynamic_world_v1": _method(
        "dynamic_world_v1", disaster_types=_ALL_TYPES, category="land_cover", result_semantics="land_cover",
        description="Klasifikasi tutupan lahan Dynamic World; bukan diagnosis kerusakan bencana.", enabled=True,
        accepted_sensors=["sentinel2", "sentinel-2"], required_bands=["label", "probability"], resolution_m=10,
        satellite="Sentinel-2 / GOOGLE/DYNAMICWORLD/V1", output_type="raster+statistics+confidence",
        validation_status="land_cover_only", limitations=[
            "Kelas tutupan lahan tidak merepresentasikan kerusakan bencana.",
            "Perubahan kelas tidak boleh ditafsirkan sebagai area terbakar, longsor, tsunami, atau kerusakan bangunan tanpa validasi.",
        ], user_label="Land Cover (Dynamic World)", data_dependencies=["GOOGLE/DYNAMICWORLD/V1"],
        multiclass=True, temporal_mode="independent_published_inference",
        classes=[{"class_id": int(k), **v} for k, v in LAND_COVER_LEGENDS["Dynamic_World"].items()],
    ),
}


def _unavailable(model_id: str, disaster_types: list[str], category: str, semantics: str, description: str,
                 reason: str, *, sensors: list[str] | None = None, source_kind: list[str] | None = None,
                 pre: bool = True, post: bool = True, output_type: str = "raster+statistics",
                 limitations: list[str] | None = None) -> dict:
    return _method(model_id, disaster_types=disaster_types, category=category, result_semantics=semantics,
                   description=description, enabled=False, accepted_sensors=sensors,
                   accepted_source_kind=source_kind, requires_pre=pre, requires_post=post,
                   output_type=output_type, availability_reason=reason, validation_status="review_required",
                   limitations=limitations)


DISASTER_MODEL_REGISTRY.update({
    "fire_dnbr_v1": _method(
        "fire_dnbr_v1", disaster_types=["forest_fire"], category="burn_severity",
        result_semantics="burn_severity_indicator",
        description="dNBR Sentinel-2 pre/post sebagai indikator burn severity.", enabled=True,
        accepted_sensors=["sentinel2", "sentinel-2"], required_bands=["B8", "B12"],
        satellite="Sentinel-2 SR Harmonized", output_type="raster+statistics",
        limitations=["dNBR bukan otomatis ground-truth luas terbakar atau kerusakan."],
        data_dependencies=["COPERNICUS/S2_SR_HARMONIZED"], user_label="Fire dNBR",
    ),
    "fire_burned_area_v1": _method(
        "fire_burned_area_v1", disaster_types=["forest_fire"], category="burned_area",
        result_semantics="burned_area_product",
        description="Produk burned-area MCD64A1/VNP64A1 yang dipisahkan dari hotspot.", enabled=True,
        accepted_source_kind=["gee"], accepted_sensors=["mcd64a1", "vnp64a1"],
        requires_pre=False, requires_post=False, output_type="raster+statistics",
        limitations=["Hotspot bukan luas terbakar; produk bulanan tidak dijumlahkan lintas produk."],
        data_dependencies=["MODIS/061/MCD64A1", "NASA/VIIRS/002/VNP64A1"], user_label="Burned Area Product",
    ),
    "fire_hotspot_observation_v1": _method(
        "fire_hotspot_observation_v1", disaster_types=["forest_fire"], category="observation",
        result_semantics="hotspot_observation",
        description="Observasi hotspot NASA FIRMS VIIRS/MODIS yang dipersistenkan bersama AnalysisResult.", enabled=True,
        accepted_source_kind=["official"], accepted_sensors=["viirs", "modis"],
        requires_pre=False, requires_post=False, output_type="feature_collection",
        limitations=["Observasi hotspot, FRP dan confidence tidak boleh dijumlahkan sebagai luas terbakar."],
        data_dependencies=["NASA FIRMS"], user_label="Fire Hotspot Observation",
        requires_earth_engine=False,
    ),
    "fire_sam_candidate_v1": _method(
        "fire_sam_candidate_v1", disaster_types=["forest_fire"], category="candidate_segmentation",
        result_semantics="candidate_object", description="Segmentasi kandidat SAM dengan seed MODIS/FIRMS.",
        enabled=True, accepted_source_kind=["gee", "local_upload"],
        accepted_sensors=["sentinel2", "sentinel-2", "viirs", "modis"], requires_pre=False, requires_post=False,
        output_type="polygon", validation_status="review_required", requires_earth_engine=False,
        data_dependencies=["SamGeo", "MODIS FireMask"], user_label="SAM Burn Candidate",
        limitations=["Hasil hanya kandidat dan wajib review; bukan model kerusakan terlatih."],
    ),
    "earthquake_building_change_v1": _unavailable("earthquake_building_change_v1", ["earthquake"], "candidate_damage", "candidate_damage",
        "Kandidat perubahan bangunan dari imagery resolusi tinggi pre/post.", "Belum ada model dan label validasi kerusakan bangunan.",
        sensors=["high_resolution", "blacksky", "planet", "maxar"], source_kind=["local_upload", "gee"], output_type="polygon",
        limitations=["Tanpa label validasi, hasil hanya candidate_damage dan review_required."]),
    "building_segmentation_v1": _unavailable("building_segmentation_v1", ["earthquake", "flood", "tsunami"], "segmentation", "building_footprint",
        "Segmentasi footprint bangunan dari model terlatih/sumber resmi.", "Model segmentasi bangunan belum tersedia.",
        sensors=["high_resolution", "blacksky", "planet", "maxar"], source_kind=["local_upload", "gee"], pre=False, output_type="polygon",
        limitations=["OSM hanya referensi dan bukan ground truth kerusakan."]),
    "road_impact_v1": _unavailable("road_impact_v1", ["earthquake", "flood", "landslide"], "candidate_damage", "road_impact_indicator",
        "Indikator perubahan/gangguan jalan dari imagery resolusi tinggi dan jaringan resmi.", "Workflow dampak jalan belum tersedia.",
        sensors=["high_resolution", "blacksky", "planet", "maxar"], source_kind=["local_upload", "gee"], output_type="polygon",
        limitations=["OSM tidak boleh dipakai sebagai ground truth kerusakan tanpa anotasi."]),
    "earthquake_intensity_v1": _unavailable("earthquake_intensity_v1", ["earthquake"], "official_observation", "official_intensity",
        "Ingest intensitas/PGA/ShakeMap dari sumber resmi.", "Ingest ShakeMap/intensitas resmi belum tersedia.",
        sensors=["shakemap", "pga"], source_kind=["official"], pre=False, post=False),
    "tsunami_inundation_v1": _unavailable("tsunami_inundation_v1", ["tsunami"], "inundation", "inundation_candidate",
        "Kandidat inundasi tsunami dari DEM, garis pantai, observasi, atau model resmi.", "Data tsunami/inundasi valid belum tersedia.",
        sensors=["sentinel1", "sentinel2", "dem", "official"], source_kind=["gee", "official", "local_upload"], output_type="raster+polygon",
        limitations=["water_segmentation bukan pengganti model tsunami."]),
    "landslide_change_v1": _unavailable("landslide_change_v1", ["landslide"], "change_detection", "landslide_change_indicator",
        "Indikator perubahan longsor dari SAR/optical, DEM, hujan, dan inventory resmi.", "Workflow longsor multi-sumber belum tersedia.",
        sensors=["sentinel1", "sentinel2", "dem", "rainfall"], source_kind=["gee", "official"],
        limitations=["Perubahan vegetasi saja tidak cukup untuk menyimpulkan longsor; false-positive warning wajib ditampilkan."]),
    "volcanic_activity_v1": _unavailable("volcanic_activity_v1", ["volcanic_eruption"], "official_observation", "volcanic_activity_indicator",
        "Observasi aktivitas gunung api dan perubahan terkait dari sumber resmi/satelit.", "Integrasi MAGMA/PVMBG belum tersedia.",
        sensors=["pvmbg", "sentinel2", "modis"], source_kind=["official", "gee"], pre=False, output_type="feature_collection+raster"),
    "storm_impact_v1": _unavailable("storm_impact_v1", ["storm"], "impact_indicator", "storm_impact_indicator",
        "Indikator dampak badai dari warning BMKG, hujan/angin, dan citra pre/post.", "Workflow dampak badai belum tersedia.",
        sensors=["bmkg", "sentinel1", "sentinel2", "high_resolution"], source_kind=["official", "gee", "local_upload"]),
    "drought_indicator_v1": _unavailable("drought_indicator_v1", ["drought"], "indicator", "drought_indicator",
        "Indikator kekeringan dari hujan, soil moisture, NDVI/EVI dan baseline time series.", "Baseline dan workflow indeks kekeringan belum tersedia.",
        sensors=["chirps", "sentinel2", "soil_moisture"], source_kind=["gee", "official"],
        limitations=["Hasil adalah drought indicator, bukan kerugian pertanian otomatis."]),
    "building_change_v1": _unavailable("building_change_v1", ["earthquake"], "damage_assessment", "candidate_damage",
        "Deteksi perubahan bangunan belum tersedia.", "Model belum dilatih.", output_type="polygon"),
    "road_damage_v1": _unavailable("road_damage_v1", ["earthquake", "flood", "landslide"], "damage_assessment", "candidate_damage",
        "Deteksi kerusakan jalan belum tersedia.", "Model belum dilatih.", output_type="polygon"),
    "other_not_available_v1": _unavailable("other_not_available_v1", ["other"], "not_available", "not_available",
        "Jenis bencana Other memerlukan subtype dan metodologi yang disetujui admin.",
        "Subtype atau metodologi bencana Other belum disetujui; model generik tidak diizinkan.",
        pre=False, post=False, output_type="not_available"),
})


def list_models(enabled_only: bool = False, disaster_type: str | None = None) -> list[dict]:
    values = list(DISASTER_MODEL_REGISTRY.values())
    if enabled_only:
        values = [model for model in values if model.get("enabled")]
    if disaster_type:
        values = [model for model in values if disaster_type in model.get("disaster_types", [])]
    return sorted(values, key=lambda model: (not model.get("enabled", False), model["model_id"]))


def get_model(model_id: str) -> dict | None:
    return DISASTER_MODEL_REGISTRY.get(model_id)


def is_enabled(model_id: str) -> bool:
    model = get_model(model_id)
    return bool(model and model.get("enabled"))
