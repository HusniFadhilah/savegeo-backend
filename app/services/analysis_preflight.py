"""Local, provider-independent input and workload checks for every analysis job."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any

from shapely.geometry import box, shape
from shapely.validation import explain_validity

AOI_KEYS = ("aoi", "geometry", "geojson", "aoi_geojson", "aoiGeojson")
MAX_AOI_BYTES = 4 * 1024 * 1024
MAX_AOI_VERTICES = 200_000
MAX_ESTIMATED_PIXELS = 250_000_000


def _find_aoi(payload: dict[str, Any]) -> Any:
    for key in AOI_KEYS:
        value = payload.get(key)
        if value:
            if isinstance(value, dict) and value.get("type") == "FeatureCollection":
                return value
            if isinstance(value, dict) and value.get("type") == "Feature":
                return value
            if isinstance(value, dict) and "geometry" in value and "type" not in value:
                return value["geometry"]
            if isinstance(value, dict):
                for nested in ("geojson", "geometry", "feature"):
                    if value.get(nested):
                        return value[nested]
            if isinstance(value, str) and not value.lstrip().startswith(("{", "[")):
                continue
            return value
    return None


def _count_vertices(geometry) -> int:
    if geometry.geom_type == "Point":
        return 1
    if geometry.geom_type in {"LineString", "LinearRing"}:
        return len(geometry.coords)
    if geometry.geom_type == "Polygon":
        return len(geometry.exterior.coords) + sum(len(ring.coords) for ring in geometry.interiors)
    if hasattr(geometry, "geoms"):
        return sum(_count_vertices(part) for part in geometry.geoms)
    return 0


def _area_ha(geometry) -> float | None:
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        return None
    from pyproj import Geod
    area_m2, _ = Geod(ellps="WGS84").geometry_area_perimeter(geometry)
    return abs(float(area_m2)) / 10_000


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def preflight(analysis_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    blocking: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: dict[str, dict[str, Any]] = {}
    now_year = datetime.now(timezone.utc).year

    aoi_value = _find_aoi(payload)
    area_ha = None
    vertices = 0
    if isinstance(aoi_value, dict) and "region" in aoi_value:
        if isinstance(aoi_value["region"], dict):
            checks["geometry"] = {"status": "deferred", "message": "Batas wilayah akan dimuat dan divalidasi oleh server."}
        else:
            blocking.append({"code": "invalid_aoi", "message": "Referensi wilayah AOI tidak valid."})
            checks["geometry"] = {"status": "fail"}
    elif aoi_value is not None:
        try:
            encoded = json.dumps(aoi_value, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_AOI_BYTES:
                raise ValueError("AOI terlalu besar; batas geometri masukan adalah 4 MiB.")
            if isinstance(aoi_value, str):
                aoi_value = json.loads(aoi_value)
            if all(k in aoi_value for k in ("west", "south", "east", "north")):
                west, south, east, north = (float(aoi_value[k]) for k in ("west", "south", "east", "north"))
                if not all(math.isfinite(v) for v in (west, south, east, north)) or not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                    raise ValueError("Batas AOI tidak valid.")
                geometry = box(west, south, east, north)
            elif aoi_value.get("type") == "FeatureCollection":
                geometry = shape({"type": "GeometryCollection", "geometries": [f["geometry"] for f in aoi_value.get("features", [])]})
            else:
                geometry = shape(aoi_value.get("geometry") if aoi_value.get("type") == "Feature" else aoi_value)
            if geometry.is_empty or not geometry.is_valid:
                raise ValueError(explain_validity(geometry) if not geometry.is_empty else "AOI kosong.")
            if geometry.bounds[0] < -180 or geometry.bounds[2] > 180 or geometry.bounds[1] < -90 or geometry.bounds[3] > 90:
                raise ValueError("Koordinat AOI harus menggunakan EPSG:4326.")
            vertices = _count_vertices(geometry)
            if vertices > MAX_AOI_VERTICES:
                raise ValueError(f"AOI memiliki {vertices:,} titik; maksimum {MAX_AOI_VERTICES:,}.")
            area_ha = _area_ha(geometry)
            checks["geometry"] = {"status": "pass", "vertices": vertices, "area_ha": round(area_ha, 3) if area_ha is not None else None}
            if vertices > 25_000:
                warnings.append({"code": "complex_aoi", "message": "AOI sangat rinci; sederhanakan batas bila proses EE lambat."})
        except (TypeError, ValueError, KeyError, AttributeError, json.JSONDecodeError) as exc:
            blocking.append({"code": "invalid_aoi", "message": str(exc)[:300]})
            checks["geometry"] = {"status": "fail"}
    else:
        known = any(payload.get(k) for k in ("aoi_name", "aoiName", "aoi_source", "aoiSource", "region", "region_code"))
        if known:
            checks["geometry"] = {"status": "deferred", "message": "Geometri akan di-resolve dari referensi wilayah server."}
        else:
            checks["geometry"] = {"status": "not_required", "message": "Tidak ada geometri eksplisit; analisis global atau server-resolved."}

    year = payload.get("year", payload.get("analysis_year", payload.get("analysisYear")))
    if year is not None:
        try:
            numeric_year = int(year)
            if not 1980 <= numeric_year <= now_year:
                raise ValueError
            checks["year"] = {"status": "pass", "value": numeric_year}
        except (TypeError, ValueError):
            blocking.append({"code": "invalid_year", "message": f"Tahun analisis harus 1980 sampai {now_year}."})
            checks["year"] = {"status": "fail"}

    today = datetime.now(timezone.utc).date()
    for first, second, label in (("start_date", "end_date", "date_range"), ("date_from", "date_to", "date_range")):
        if payload.get(first) is not None or payload.get(second) is not None:
            start, end = _parse_date(payload.get(first)), _parse_date(payload.get(second))
            if start is None or end is None or end < start or start > today:
                blocking.append({"code": "invalid_date_range", "message": "Tanggal awal/akhir tidak valid atau tanggal awal berada di masa depan."})
                checks[label] = {"status": "fail"}
            else:
                checks[label] = {"status": "pass", "start": start.isoformat(), "end": end.isoformat()}
                if end > today:
                    warnings.append({"code": "future_date_range_end", "message": "Rentang mencakup tanggal mendatang; hasil hanya dapat memuat data yang sudah tersedia."})
                    checks[label]["status"] = "warning"
            break

    cloud = payload.get("cloud", payload.get("cloud_cover", payload.get("cloudCover", payload.get("cloud_threshold"))))
    if cloud is not None:
        try:
            value = float(cloud)
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError
            checks["cloud_threshold"] = {"status": "pass", "value": value}
        except (TypeError, ValueError):
            blocking.append({"code": "invalid_cloud_threshold", "message": "Ambang awan harus antara 0 dan 100 persen."})
            checks["cloud_threshold"] = {"status": "fail"}

    scale = payload.get("scale", payload.get("resolution", payload.get("scale_m")))
    if scale is not None:
        try:
            value = float(scale)
            if not math.isfinite(value) or not 0.25 <= value <= 100_000:
                raise ValueError
            checks["resolution"] = {"status": "pass", "meters": value}
        except (TypeError, ValueError):
            blocking.append({"code": "invalid_resolution", "message": "Resolusi analisis harus 0,25 sampai 100.000 meter."})
            checks["resolution"] = {"status": "fail"}
        else:
            if area_ha is not None and area_ha * 10_000 / value**2 > MAX_ESTIMATED_PIXELS:
                warnings.append({"code": "large_raster_request", "message": "Perkiraan raster melebihi 250 juta piksel; gunakan skala lebih kasar atau AOI bertahap."})
                checks["estimated_pixels"] = {"status": "warning", "limit": MAX_ESTIMATED_PIXELS}

    if not checks:
        checks["parameters"] = {"status": "not_checked"}
    return {
        "schema_version": "analysis-preflight-v1", "analysis_type": analysis_type,
        "status": "blocked" if blocking else "warning" if warnings else "ready",
        "blocking": blocking, "warnings": warnings, "checks": checks,
        "provider_checks": {"status": "deferred", "message": "Kredensial dan ketersediaan katalog diperiksa saat worker menjalankan analisis."},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def assess_result_quality(analysis_type: str, result: dict[str, Any]) -> dict[str, Any]:
    """Summarize quality signals actually returned by a provider/model, without inventing absent metrics."""
    metrics: list[dict[str, Any]] = []
    def visit(value: Any, path: str = "result") -> None:
        if isinstance(value, dict):
            for key in ("valid_pixel_pct", "coverage_pct", "coverage_percent", "cloud_free_pct",
                        "images_used", "sample_count", "no_data_count", "valid_fraction"):
                item = value.get(key)
                if isinstance(item, (int, float)) and math.isfinite(float(item)):
                    metrics.append({"path": f"{path}.{key}", "name": key, "value": item})
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value[:1000]):
                visit(child, f"{path}[{index}]")
    visit(result)
    warnings: list[dict[str, str]] = []
    for metric in metrics:
        name, value = metric["name"], float(metric["value"])
        if name in {"valid_pixel_pct", "coverage_pct", "coverage_percent", "cloud_free_pct"} and value < 50:
            warnings.append({"code": "low_data_coverage", "message": f"{metric['path']} bernilai {value:g}; tinjau cakupan sebelum interpretasi."})
        if name in {"images_used", "sample_count"} and value <= 0:
            warnings.append({"code": "empty_observation_support", "message": f"{metric['path']} tidak berisi pengamatan valid."})
    return {"schema_version": "analysis-quality-v1", "analysis_type": analysis_type,
            "status": "review_required" if warnings else "signals_available" if metrics else "not_reported",
            "metrics": metrics[:200], "warnings": warnings,
            "note": "Hanya indikator yang dikembalikan sumber analisis yang dicatat; ketiadaan metrik bukan bukti mutu lulus."}
