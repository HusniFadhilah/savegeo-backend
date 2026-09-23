"""Persisted wildfire products.

The interactive wildfire endpoints remain useful for exploration, but an admin
run must also retain the exact period, sources, and product semantics in the
normal disaster analysis tables.  This module is deliberately an adapter: it
does not turn hotspot counts into area and it never publishes a missing
product as zero.
"""
from __future__ import annotations

import datetime as dt

from app.services import disaster_service, fire_multi_source_service, samgeo_service, wildfire_hotspot_service
from app.services.gee_common import AnalysisError


def _date(value: object, fallback: dt.date | None) -> dt.date | None:
    if value is None or value == "":
        return fallback
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise AnalysisError("Tanggal produk karhutla harus berupa YYYY-MM-DD", 400) from exc


def _period(event, parameters: dict, *, max_days: int | None = None) -> tuple[dt.date, dt.date]:
    start = _date(
        parameters.get("start_date"),
        getattr(event, "monitoring_from", None) or getattr(event, "start_date", None),
    )
    end = _date(
        parameters.get("end_date"),
        getattr(event, "monitoring_to", None) or getattr(event, "end_date", None),
    )
    if start is None or end is None:
        raise AnalysisError("Periode produk karhutla belum diisi", 400)
    if end < start:
        raise AnalysisError("end_date tidak boleh lebih awal dari start_date", 400)
    if end > dt.datetime.now(dt.UTC).date():
        raise AnalysisError("Periode produk karhutla tidak boleh berada di masa depan", 400)
    if max_days is not None and (end - start).days > max_days:
        raise AnalysisError(f"Periode produk ini maksimal {max_days + 1} hari", 400)
    return start, end


def _aoi_payload(aoi) -> dict:
    return {"geojson": aoi.geojson}


def _hotspot_output(db, event, aoi, parameters: dict) -> dict:
    start, end = _period(event, parameters)
    sync = wildfire_hotspot_service.sync_event_hotspots(db, event, start, end)
    features = wildfire_hotspot_service.list_event_features(db, event.id, start, end)
    summary = wildfire_hotspot_service.summarize(features)
    return {
        "tile_url": None,
        "statistics": {
            "observation_count": len(features),
            "summary": summary,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "sync": sync,
            "product": "NASA FIRMS hotspot observation",
            "area_ha": None,
        },
        "features": {"type": "FeatureCollection", "features": features},
        "legend": [],
        "confidence_summary": None,
    }


def _burned_area_output(event, aoi, parameters: dict) -> dict:
    start, end = _period(event, parameters, max_days=30)
    requested = parameters.get("sources") or ["mcd64a1", "vnp64a1"]
    if not isinstance(requested, list) or not requested or any(source not in fire_multi_source_service.BURN_SOURCES for source in requested):
        raise AnalysisError("sources burned-area hanya boleh mcd64a1 atau vnp64a1", 400)
    loaded = fire_multi_source_service.load_sources(
        {"aoi": _aoi_payload(aoi), "start_date": start.isoformat(), "end_date": end.isoformat(), "sources": requested},
        ee_available=True,
    )
    products = [source for source in loaded["sources"] if source.get("status") == "ok" and source.get("kind") == "burned_area"]
    if not products:
        statuses = [{"id": source.get("id"), "status": source.get("status"), "message": source.get("message")} for source in loaded["sources"]]
        raise AnalysisError(f"Produk burned-area belum tersedia untuk periode/AOI ini: {statuses}", 404)
    primary = products[0]
    return {
        "tile_url": primary.get("tile_url"),
        "statistics": {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "products": [
                {key: value for key, value in product.items() if key not in {"tile_url"}}
                for product in products
            ],
            "area_ha": primary.get("area_ha"),
            "product": primary.get("source"),
        },
        "features": None,
        "legend": [],
        "confidence_summary": None,
    }


def _dnbr_output(event, aoi, pre_image, post_image, parameters: dict) -> dict:
    if pre_image is None or post_image is None:
        raise AnalysisError("dNBR membutuhkan imagery Sentinel-2 pre dan post", 400)
    payload = {
        "aoi": _aoi_payload(aoi),
        "event_type": "fire",
        "before_start": pre_image.acquisition_date.isoformat(),
        "before_end": pre_image.acquisition_date.isoformat(),
        "after_start": post_image.acquisition_date.isoformat(),
        "after_end": post_image.acquisition_date.isoformat(),
        "dnbr_threshold": parameters.get("dnbr_threshold", 0.27),
        "scale": parameters.get("scale", 20),
    }
    mapped = disaster_service.get_disaster_event_map(payload)
    if not mapped.get("tile_url") or mapped.get("area_ha") is None:
        raise AnalysisError("dNBR tidak menghasilkan tile dan statistik area yang valid", 422)
    statistics = {
        key: value for key, value in mapped.items()
        if key not in {"success", "tile_url", "legend", "hotspots", "title", "source", "method_note"}
    }
    statistics["product"] = "Sentinel-2 SR Harmonized dNBR"
    return {
        "tile_url": mapped["tile_url"],
        "statistics": statistics,
        "features": mapped.get("hotspots"),
        "legend": mapped.get("legend") or [],
        "confidence_summary": None,
    }


def _sam_candidate_output(parameters: dict) -> dict:
    job_id = str(parameters.get("job_id") or "").strip()
    if not job_id:
        raise AnalysisError("SAM candidate memerlukan parameters.job_id dari job yang sudah selesai", 400)
    job = samgeo_service.get_job(job_id)
    if job.get("status") != "complete" or not isinstance(job.get("result"), dict):
        raise AnalysisError("Job SAM belum selesai atau hasilnya tidak tersedia", 409)
    result = job["result"]
    features = result if result.get("type") == "FeatureCollection" else None
    if features is None:
        raise AnalysisError("Hasil SAM bukan FeatureCollection", 422)
    metadata = result.get("metadata") or {}
    return {
        "tile_url": None,
        "statistics": {
            "candidate_count": len(features.get("features") or []),
            "metadata": metadata,
            "product": "MODIS-seeded SamGeo candidate",
        },
        "features": features,
        "legend": [],
        "confidence_summary": None,
        "requires_review": True,
    }


def compute_persisted_product(db, event, aoi, pre_image, post_image, model_id: str, parameters: dict | None = None) -> dict:
    parameters = dict(parameters or {})
    if model_id == "fire_hotspot_observation_v1":
        return _hotspot_output(db, event, aoi, parameters)
    if model_id == "fire_burned_area_v1":
        return _burned_area_output(event, aoi, parameters)
    if model_id == "fire_dnbr_v1":
        return _dnbr_output(event, aoi, pre_image, post_image, parameters)
    if model_id == "fire_sam_candidate_v1":
        return _sam_candidate_output(parameters)
    raise AnalysisError("Produk karhutla belum memiliki dispatcher persisted", 400)
