"""Read-only, bounded adapters for independent wildfire observations.

Thermal observations, burn maps, administrative boundaries and hazard indices
are intentionally separate. Missing keys/data do not discard other sources.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from collections import defaultdict
from xml.etree import ElementTree
from uuid import uuid4

import ee
import requests

from app.core.config import get_settings
from app.services import imagery_service
from app.services.gee_common import AnalysisError, _event_area_ha, create_geometry_from_payload, get_tile_url

FIRMS_SOURCES = {
    "firms_noaa20": ("VIIRS_NOAA20_NRT", "VIIRS NOAA-20", 375),
    "firms_noaa21": ("VIIRS_NOAA21_NRT", "VIIRS NOAA-21", 375),
    "firms_snpp": ("VIIRS_SNPP_NRT", "VIIRS S-NPP", 375),
    "firms_modis": ("MODIS_NRT", "MODIS Terra/Aqua", 1000),
}
BURN_SOURCES = {
    "mcd64a1": ("MODIS/061/MCD64A1", "BurnDate", "NASA MODIS MCD64A1", "#be123c"),
    "vnp64a1": ("NASA/VIIRS/002/VNP64A1", "Burn_Date", "NASA VIIRS VNP64A1", "#ea580c"),
}
BIG_LAYER = "https://geoservices.big.go.id/gis/rest/services/DISIGT/BatasWilayah/MapServer/0"
BMKG_LAYER = "https://datacuaca.bmkg.go.id/arcgis/rest/services/production/geohotspot/MapServer/0"
CDSE_ROOT = "https://stac.dataspace.copernicus.eu/v1"
INARISK_ROOT = "https://gis.bnpb.go.id/server/rest/services/inarisk/INDEKS_BAHAYA_KARHUTLA/ImageServer"
MAX_POINTS = 2000


def _period(data: dict) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(data["start_date"]), date.fromisoformat(data["end_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalysisError("Tanggal harus berupa YYYY-MM-DD", 400) from exc
    if end < start or (end - start).days > 30:
        raise AnalysisError("Periode multi-sumber maksimal 31 hari dan akhir tidak boleh sebelum awal", 400)
    if end > datetime.now(UTC).date():
        raise AnalysisError("Tanggal observasi tidak boleh berada di masa depan", 400)
    return start, end


def _ring_contains(point: list, ring: list) -> bool:
    x, y = point[:2]
    inside = False
    if not ring:
        return False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous[:2]
        x2, y2 = current[:2]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def _contains(point: list, geojson: dict) -> bool:
    kind = geojson.get("type")
    if kind == "FeatureCollection":
        return any(_contains(point, feature) for feature in geojson.get("features", []))
    if kind == "Feature":
        return _contains(point, geojson.get("geometry") or {})
    if kind == "Polygon":
        rings = geojson.get("coordinates") or []
        return (
            bool(rings)
            and _ring_contains(point, rings[0])
            and not any(_ring_contains(point, ring) for ring in rings[1:])
        )
    if kind == "MultiPolygon":
        return any(
            _contains(point, {"type": "Polygon", "coordinates": polygon})
            for polygon in geojson.get("coordinates", [])
        )
    return False


def clip_points(features: list[dict], aoi: dict) -> list[dict]:
    geometry = aoi.get("geojson", aoi)
    bbox = imagery_service._aoi_payload_to_bbox(aoi)
    result = []
    for feature in features:
        point = (feature.get("geometry") or {}).get("coordinates")
        if (feature.get("geometry") or {}).get("type") != "Point" or not point or len(point) < 2:
            continue
        lon, lat = map(_float, point[:2])
        if lon is None or lat is None:
            continue
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        if geometry.get("type") and not _contains([lon, lat], geometry):
            continue
        result.append(feature)
    return result


def _float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_firms_csv(content: str, source_id: str) -> list[dict]:
    _, sensor, resolution = FIRMS_SOURCES[source_id]
    features = []
    for row in csv.DictReader(io.StringIO(content.lstrip("\ufeff"))):
        lon, lat = _float(row.get("longitude")), _float(row.get("latitude"))
        if lon is None or lat is None:
            continue
        acq_time = str(row.get("acq_time") or "0000").zfill(4)
        acquired_at = f"{row.get('acq_date')}T{acq_time[:2]}:{acq_time[2:4]}:00Z"
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "source_id": source_id,
                    "source": "NASA FIRMS",
                    "sensor": sensor,
                    "acquired_at": acquired_at,
                    "confidence": row.get("confidence"),
                    "frp_mw": _float(row.get("frp")),
                    "brightness_k": _float(row.get("bright_ti4") or row.get("brightness")),
                    "resolution_m": resolution,
                    "daynight": row.get("daynight"),
                    "satellite": row.get("satellite"),
                    "name": f"{sensor} · {row.get('acq_date')} {acq_time}",
                    "original": row,
                },
            }
        )
    return features


def _fetch_firms(source_id: str, bbox: list, start: date, end: date) -> dict:
    settings = get_settings()
    if not settings.nasa_firms_map_key:
        return {
            "id": source_id,
            "status": "needs_key",
            "message": "Isi NASA_FIRMS_MAP_KEY di backend; key tidak dikirim ke browser.",
            "features": [],
        }
    source, label, _ = FIRMS_SOURCES[source_id]
    features = []
    cursor = start
    while cursor <= end:
        days = min(5, (end - cursor).days + 1)
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{settings.nasa_firms_map_key}/{source}/{','.join(map(str, bbox))}/{days}/{cursor.isoformat()}"
        response = requests.get(url, timeout=25)
        response.raise_for_status()
        if "latitude" not in response.text[:600]:
            raise AnalysisError(
                "FIRMS tidak mengembalikan CSV observasi; cek key dan ketersediaan tanggal", 502
            )
        features.extend(parse_firms_csv(response.text, source_id))
        cursor += timedelta(days=days)
    return {
        "id": source_id,
        "label": label,
        "status": "ok",
        "features": features,
        "message": "Deteksi piksel termal NRT, bukan jumlah kejadian kebakaran.",
    }


def _fetch_bmkg(bbox: list, start: date, end: date) -> dict:
    end_exclusive = end + timedelta(days=1)
    response = requests.get(
        f"{BMKG_LAYER}/query",
        params={
            "where": f"date_full >= TIMESTAMP '{start.isoformat()} 00:00:00' AND date_full < TIMESTAMP '{end_exclusive.isoformat()} 00:00:00'",
            "geometry": ",".join(map(str, bbox)),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": 4326,
            "outFields": "*",
            "returnGeometry": "true",
            "resultRecordCount": MAX_POINTS,
            "orderByFields": "date_full DESC",
            "f": "geojson",
        },
        timeout=30,
    )
    if response.status_code == 403:
        raise AnalysisError(
            "BMKG menolak akses otomatis (HTTP 403); gunakan portal resmi atau impor data yang diunduh secara sah.",
            502,
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise AnalysisError("Layanan BMKG menolak query tanggal/geometri", 502)
    features = payload.get("features", [])
    for feature in features:
        props = feature.setdefault("properties", {})
        props["original"] = dict(props)
        timestamp = props.get("date_full")
        acquired_at = (
            datetime.fromtimestamp(timestamp / 1000, UTC).isoformat()
            if isinstance(timestamp, (int, float))
            else timestamp
        )
        props.update(
            {
                "source_id": "bmkg",
                "source": "BMKG Geohotspot",
                "sensor": "Himawari Geohotspot",
                "acquired_at": acquired_at,
                "confidence": None,
                "frp_mw": None,
                "name": f"BMKG · {props.get('kabupaten') or props.get('provinsi') or 'Hotspot'}",
            }
        )
    return {
        "id": "bmkg",
        "status": "ok",
        "features": features,
        "truncated": bool(payload.get("exceededTransferLimit")),
        "message": "Arsip mengikuti retensi layanan BMKG; kosong tidak berarti tidak ada kebakaran.",
    }


def _timestamp(value) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
    except (ValueError, TypeError):
        return None


def _distance(a: list, b: list) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, math.radians(b[0] - a[0])
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0, 1 - value)))


def merge_observations(features: list[dict]) -> list[dict]:
    """Fuse cross-source observations within 500m/30min, never fire events."""
    groups = []
    # Kalimantan is near the equator. A 0.01-degree cell exceeds 500m;
    # adjacent cells cover all possible matches without a quadratic scan.
    buckets = defaultdict(list)
    for feature in features:
        props = feature.get("properties") or {}
        timestamp = _timestamp(props.get("acquired_at"))
        match = None
        lon, lat = feature["geometry"]["coordinates"][:2]
        cell = (math.floor(lon / 0.01), math.floor(lat / 0.01))
        candidates = (
            group for dx in (-1, 0, 1) for dy in (-1, 0, 1) for group in buckets[(cell[0] + dx, cell[1] + dy)]
        )
        for group in candidates:
            previous = group["properties"]
            if props.get("source_id") in previous["source_ids"]:
                continue
            group_time = _timestamp(previous.get("acquired_at"))
            if timestamp is None or group_time is None or abs(timestamp - group_time) > 1800:
                continue
            if _distance(feature["geometry"]["coordinates"], group["geometry"]["coordinates"]) <= 500:
                match = group
                break
        if match:
            previous = match["properties"]
            previous["source_ids"].append(props.get("source_id"))
            previous["observations"].append(dict(props))
            previous["observation_count"] += 1
            values = [value for value in (previous.get("frp_mw"), props.get("frp_mw")) if value is not None]
            previous["frp_mw"] = max(values) if values else None
        else:
            group = {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    **props,
                    "source_ids": [props.get("source_id")],
                    "observations": [dict(props)],
                    "observation_count": 1,
                },
            }
            groups.append(group)
            buckets[cell].append(group)
    return groups


def _fetch_cdse(bbox: list, start: date, end: date) -> dict:
    response = requests.post(
        f"{CDSE_ROOT}/search",
        json={
            "collections": ["sentinel-2-l2a"],
            "bbox": bbox,
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            "limit": 20,
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    scenes = []
    for item in payload.get("features", []):
        props = item.get("properties") or {}
        scenes.append(
            {
                "type": "Feature",
                "geometry": item.get("geometry"),
                "properties": {
                    "source": "Copernicus CDSE",
                    "source_id": "cdse",
                    "scene_id": item.get("id"),
                    "acquired_at": props.get("datetime"),
                    "cloud_cover_pct": props.get("eo:cloud_cover"),
                    "name": item.get("id"),
                    "item_url": f"{CDSE_ROOT}/collections/sentinel-2-l2a/items/{item.get('id')}",
                },
            }
        )
    return {
        "id": "cdse",
        "status": "ok",
        "features": scenes,
        "kind": "footprints",
        "truncated": any(link.get("rel") == "next" for link in payload.get("links", [])),
        "message": "Footprint/katalog scene, bukan raster RGB; pixel CDSE memerlukan token/akses S3. dNBR memakai Earth Engine.",
    }


def _fetch_burn(source_id: str, aoi: dict, start: date, end: date, ee_available: bool) -> dict:
    if not ee_available:
        return {"id": source_id, "status": "unavailable", "message": "Earth Engine belum aktif."}
    collection_id, band, label, color = BURN_SOURCES[source_id]
    month_start = start.replace(day=1)
    next_month = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
    collection = ee.ImageCollection(collection_id).filterDate(month_start.isoformat(), next_month.isoformat())
    count = collection.size().getInfo()
    if not count:
        return {
            "id": source_id,
            "status": "no_data",
            "message": "Produk bulanan periode ini belum tersedia. Tidak memakai bulan lama sebagai pengganti.",
        }
    available_months = sorted(
        {
            datetime.fromtimestamp(value / 1000, UTC).strftime("%Y-%m")
            for value in collection.aggregate_array("system:time_start").getInfo()
        }
    )
    requested_months = []
    cursor = month_start
    while cursor < next_month:
        requested_months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    missing_months = sorted(set(requested_months) - set(available_months))
    geometry = create_geometry_from_payload(aoi)

    def burn_mask(image):
        burn_date = image.select(band)
        year = ee.Date(image.get("system:time_start")).get("year")
        jan1 = ee.Date.fromYMD(year, 1, 1)
        first_day = ee.Date(start.isoformat()).difference(jan1, "day").add(1)
        last_day = ee.Date(end.isoformat()).difference(jan1, "day").add(1)
        qa = image.select("QA")
        return (
            burn_date.gte(first_day)
            .And(burn_date.lte(last_day))
            .And(burn_date.gt(0))
            .And(qa.bitwiseAnd(2).neq(0))
        )

    mask = collection.map(burn_mask).max().selfMask().rename("area").clip(geometry)
    tile = get_tile_url(mask, {"min": 1, "max": 1, "palette": [color]}, label)
    return {
        "id": source_id,
        "status": "ok",
        "kind": "burned_area",
        "tile_url": tile["tile_url"] if tile else None,
        "area_ha": _event_area_ha(mask, geometry, 500),
        "resolution_m": 500,
        "source": label,
        "available_months": available_months,
        "missing_months": missing_months,
        "message": "Validasi area bulanan 500m; tidak dijumlahkan dengan luas dNBR atau antarproduk."
        + (f" Data sebagian: bulan {', '.join(missing_months)} belum terbit." if missing_months else ""),
    }


def _fetch_inarisk() -> dict:
    # ArcGIS OGC services use /services, not /rest/services.
    url = f"{INARISK_ROOT.replace('/rest/', '/')}/WMSServer"
    response = requests.get(url, params={"request": "GetCapabilities", "service": "WMS"}, timeout=20)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    layers = [node.text for node in root.iter() if node.tag.split("}")[-1] == "Name" and node.text != "WMS"]
    if not layers:
        raise AnalysisError("Layer WMS InaRISK belum tersedia", 502)
    return {
        "id": "inarisk",
        "status": "ok",
        "kind": "hazard",
        "source": "BNPB InaRISK",
        "wms_url": url,
        "wms_layers": layers[-1],
        "message": "Indeks bahaya 100m, bukan luas/titik kejadian kebakaran saat ini; tahun peta mengikuti penyedia.",
    }


def load_sources(data: dict, ee_available: bool = False) -> dict:
    if not isinstance(data, dict):
        raise AnalysisError("Request harus berupa object", 400)
    start, end = _period(data)
    aoi = data.get("aoi") or {}
    bbox = imagery_service._aoi_payload_to_bbox(aoi)
    if not (
        all(math.isfinite(value) for value in bbox)
        and 108 <= bbox[0] < bbox[2] <= 120
        and -5 <= bbox[1] < bbox[3] <= 5
    ):
        raise AnalysisError(
            "AOI multi-sumber harus berada di wilayah Kalimantan (108–120 BT, 5 LS–5 LU)", 400
        )
    allowed = set(FIRMS_SOURCES) | set(BURN_SOURCES) | {"bmkg", "cdse", "inarisk"}
    requested = data.get(
        "sources",
        ["firms_noaa20", "firms_noaa21", "firms_modis", "bmkg", "cdse", "mcd64a1", "vnp64a1", "inarisk"],
    )
    if (
        not isinstance(requested, list)
        or not requested
        or any(not isinstance(source, str) for source in requested)
    ):
        raise AnalysisError("Pilih setidaknya satu sumber", 400)
    requested = list(dict.fromkeys(requested))
    if any(source not in allowed for source in requested):
        raise AnalysisError("Sumber karhutla tidak dikenal", 400)
    results = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        tasks = {}
        for source in requested:
            if source in FIRMS_SOURCES:
                task = executor.submit(_fetch_firms, source, bbox, start, end)
            elif source == "bmkg":
                task = executor.submit(_fetch_bmkg, bbox, start, end)
            elif source == "cdse":
                task = executor.submit(_fetch_cdse, bbox, start, end)
            elif source in BURN_SOURCES:
                task = executor.submit(_fetch_burn, source, aoi, start, end, ee_available)
            else:
                task = executor.submit(_fetch_inarisk)
            tasks[task] = source
        for task in as_completed(tasks):
            source = tasks[task]
            try:
                result = task.result()
                if source in FIRMS_SOURCES or source == "bmkg":
                    clipped = clip_points(result.get("features", []), aoi)
                    result["truncated"] = bool(result.get("truncated")) or len(clipped) > MAX_POINTS
                    result["features"] = clipped[:MAX_POINTS]
                results[source] = result
            except AnalysisError as exc:
                results[source] = {"id": source, "status": "error", "message": str(exc), "features": []}
            except requests.Timeout:
                results[source] = {
                    "id": source,
                    "status": "error",
                    "message": "Layanan sumber melewati batas waktu; sumber lain tetap dimuat.",
                    "features": [],
                }
            except Exception:
                # Never expose requests exception URLs: FIRMS embeds its key in the path.
                results[source] = {
                    "id": source,
                    "status": "error",
                    "message": "Sumber gagal dimuat; cek layanan, kredensial, dan periode. Sumber lain tetap tersedia.",
                    "features": [],
                }
    raw = [
        feature
        for source in requested
        if source in FIRMS_SOURCES or source == "bmkg"
        for feature in results[source].get("features", [])
    ]
    merged = merge_observations(raw)
    return {
        "sources": [results[source] for source in requested],
        "hotspots": {"type": "FeatureCollection", "features": merged},
        "raw_count": len(raw),
        "merged_count": len(merged),
        "generated_at": datetime.now(UTC).isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "note": "Penggabungan 500m/30menit hanya mengurangi deteksi lintas sumber, bukan menghitung kejadian kebakaran. Metadata asli dipertahankan.",
    }


def _admin_name(value: str) -> str:
    return re.sub(r"^(provinsi|kabupaten|kab\.?|kota)\s+", "", value.strip().lower()).strip()


def load_big_boundaries(data: dict) -> dict:
    if not isinstance(data, dict):
        raise AnalysisError("Request harus berupa object", 400)
    province = str(data.get("province") or "").strip().upper()
    if province not in {
        "KALIMANTAN BARAT",
        "KALIMANTAN TENGAH",
        "KALIMANTAN SELATAN",
        "KALIMANTAN TIMUR",
        "KALIMANTAN UTARA",
    }:
        raise AnalysisError("Pilih satu provinsi Kalimantan", 400)
    response = requests.get(
        f"{BIG_LAYER}/query",
        params={
            "where": f"UPPER(wadmpr)='{province}'",
            "outFields": "namobj,wadmpr,wadmkk,luas_ha",
            "outSR": 4326,
            "returnGeometry": "true",
            "f": "geojson",
        },
        timeout=40,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error") or not payload.get("features"):
        raise AnalysisError("Batas BIG provinsi ini belum dapat dimuat", 502)
    city = str(data.get("city") or "").strip()
    if city:
        payload["features"] = [
            feature
            for feature in payload["features"]
            if _admin_name(city)
            in {
                _admin_name(str((feature.get("properties") or {}).get("namobj") or "")),
                _admin_name(str((feature.get("properties") or {}).get("wadmkk") or "")),
            }
        ]
        if not payload["features"]:
            raise AnalysisError("Nama kabupaten/kota tidak ditemukan pada batas BIG", 404)
    for feature in payload["features"]:
        props = feature.setdefault("properties", {})
        props.update({"name": props.get("namobj"), "source": "BIG", "province": props.get("wadmpr")})
    return {"type": "FeatureCollection", "features": payload["features"], "source": BIG_LAYER}


def import_observations(data: dict) -> dict:
    if not isinstance(data, dict):
        raise AnalysisError("Request harus berupa object", 400)
    content = data.get("content")
    if not isinstance(content, str) or len(content.encode("utf-8")) > 2_000_000:
        raise AnalysisError("Impor maksimal 2 MB", 400)
    source = str(data.get("source") or "SIPONGI import")[:100]
    if data.get("format") == "geojson":
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise AnalysisError("GeoJSON tidak valid", 400) from exc
        if not isinstance(payload, dict):
            raise AnalysisError("GeoJSON harus berupa FeatureCollection atau Feature", 400)
        features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]
    else:
        features = []
        try:
            dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        for row in csv.DictReader(io.StringIO(content.lstrip("\ufeff")), dialect=dialect):
            normalized = {str(key).strip().lower(): value for key, value in row.items() if key}
            lon = next(
                (
                    _float(normalized[key])
                    for key in ("longitude", "lon", "lng", "bujur")
                    if key in normalized
                ),
                None,
            )
            lat = next(
                (_float(normalized[key]) for key in ("latitude", "lat", "lintang") if key in normalized), None
            )
            if lon is not None and lat is not None:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                        "properties": normalized,
                    }
                )
            if len(features) > 5000:
                raise AnalysisError("Impor maksimal 5.000 fitur", 400)
    if not isinstance(features, list) or not features or len(features) > 5000:
        raise AnalysisError(
            "File tidak memiliki titik koordinat/polygon. CSV agregat jumlah hotspot tidak dapat dipetakan sebagai titik.",
            400,
        )
    batch_id = uuid4().hex
    for feature in features:
        if (
            not isinstance(feature, dict)
            or feature.get("type") != "Feature"
            or not isinstance(feature.get("geometry"), dict)
            or feature["geometry"].get("type") not in {"Point", "Polygon", "MultiPolygon"}
        ):
            raise AnalysisError("Impor hanya mendukung fitur Point/Polygon/MultiPolygon", 400)
        geometry = feature["geometry"]

        def valid_position(position):
            return (
                isinstance(position, list)
                and len(position) >= 2
                and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                    for v in position
                )
                and -180 <= position[0] <= 180
                and -90 <= position[1] <= 90
            )

        def valid_polygon(rings):
            return (
                isinstance(rings, list)
                and bool(rings)
                and all(
                    isinstance(ring, list)
                    and len(ring) >= 4
                    and all(valid_position(p) for p in ring)
                    and ring[0] == ring[-1]
                    for ring in rings
                )
            )

        coords = geometry.get("coordinates")
        valid = (
            valid_position(coords)
            if geometry["type"] == "Point"
            else valid_polygon(coords)
            if geometry["type"] == "Polygon"
            else isinstance(coords, list) and bool(coords) and all(valid_polygon(p) for p in coords)
        )
        if not valid or not isinstance(feature.get("properties") or {}, dict):
            raise AnalysisError("Koordinat/properties GeoJSON tidak valid", 400)
        props = feature.setdefault("properties", {}) or {}
        props.update(
            {
                "source": source,
                "source_id": "import",
                "verification": "user_import_not_verified",
                "import_batch_id": batch_id,
            }
        )
        feature["properties"] = props
    return {
        "type": "FeatureCollection",
        "features": features,
        "source": source,
        "note": "Impor lokal sesi pengguna; tidak otomatis menjadi data kejadian terverifikasi.",
    }
