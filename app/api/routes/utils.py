"""Utility endpoints - /api/utils/*. Ported from backend/utils_routes.py."""
from __future__ import annotations

import base64
import json
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.isolated_process import run_isolated
from app.core.http import SHP_JSON_BODY_LIMIT_BYTES
from app.core.shapefile_limits import SHP_MAX_MEMBER_BYTES
from app.core.security import get_current_app_viewer
from app.services import geocode_service
from app.services.location_provider import search_places, imagery_date_metadata

# Geocoding proxies a third-party service under SAVEGEO's identity and the
# converters parse uploaded archives, so every utility requires a signed-in user.
router = APIRouter(prefix="/utils", tags=["utils"], dependencies=[Depends(get_current_app_viewer)])
logger = logging.getLogger(__name__)

_UA = "GeoMoka/1.0 (geospatial platform; contact: admin@geomoka.id)"

# The worker validates archive entries and expanded size again before parsing.
_SHP_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Retained for callers and regression tests; the worker enforces the same limit.
_SHP_MAX_MEMBER_BYTES = SHP_MAX_MEMBER_BYTES


@router.get("/geocode")
def geocode(q: str = Query(default="", max_length=200)):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Parameter q wajib diisi.")

    results = search_places(q, limit=3)

    if not results:
        raise HTTPException(status_code=404, detail=f"Lokasi {q!r} tidak ditemukan.")

    d = results[0]
    bb = d.get("boundingbox", [])  # [south, north, west, east]
    bbox = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])] if len(bb) == 4 else None

    addr = d.get("address", {})
    country_code = addr.get("country_code", "")
    province = addr.get("state") or addr.get("province") or addr.get("region") or ""
    city = addr.get("city") or addr.get("county") or addr.get("municipality") or addr.get("town") or addr.get("village") or ""
    district = addr.get("suburb") or addr.get("city_district") or ""
    village = addr.get("neighbourhood") or addr.get("quarter") or addr.get("hamlet") or ""

    return {
        "lat": float(d["lat"]),
        "lng": float(d["lon"]),
        "display_name": d.get("display_name", q),
        "type": d.get("type", ""),
        "osm_type": d.get("osm_type", ""),
        "bbox": bbox,
        "address_info": {
            "country_code": country_code,
            "province": province,
            "city": city,
            "district": district,
            "village": village,
        },
        "alternatives": [
            {"lat": float(x["lat"]), "lng": float(x["lon"]), "display_name": x.get("display_name", "")}
            for x in results[1:]
        ],
    }


@router.get("/geocode/search")
def geocode_search(q: str = Query(default="", max_length=200), limit: int = Query(default=8, ge=1, le=10)):
    """Multi-result geocoding for the async region/address search box (AOI
    'Seluruh Indonesia' tab) - distinct from /geocode above, which returns a
    single best match + up to 2 alternatives for the chatbot's "fly to this
    place" flow. This one returns up to `limit` candidates as the user types,
    restricted to Indonesia, each carrying `geojson` (the real admin-boundary
    polygon, when Nominatim has one) so the frontend can use actual
    boundaries instead of just a bounding-box rectangle for a picked region."""
    q = q.strip()
    if not q or len(q) < 3:
        return {"results": []}

    limit = max(1, min(limit, 10))
    results = search_places(q, limit=limit, polygon=True)

    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="Layanan pencarian lokasi mengembalikan respons tidak valid.")

    normalized = [c for c in (geocode_service.normalize_candidate(d) for d in results if isinstance(d, dict)) if c]
    return {"results": geocode_service.rank_candidates(normalized, q)}


@router.get("/imagery-date")
def imagery_date(lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
                 lng: float = Query(ge=-180, le=180, allow_inf_nan=False)):
    return imagery_date_metadata(lat, lng)


class ParseAoiRequest(BaseModel):
    text: str = ""
    name: str | None = "Custom AOI"


@router.post("/parse_aoi")
def parse_aoi(payload: ParseAoiRequest):
    text = payload.text.strip()
    name = (payload.name or "Custom AOI").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text wajib diisi.")

    try:
        geo = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Bukan JSON valid: {e}")

    gtype = geo.get("type", "")

    if gtype == "FeatureCollection":
        features = geo.get("features", [])
        if not features:
            raise HTTPException(status_code=400, detail="FeatureCollection kosong.")
        geo = features[0]
        gtype = geo.get("type", "")

    if gtype in ("Polygon", "MultiPolygon"):
        geo = {"type": "Feature", "geometry": geo, "properties": {"name": name}}

    if geo.get("type") != "Feature":
        raise HTTPException(status_code=400, detail=f"Tipe GeoJSON tidak didukung: {gtype}")

    geom_type = (geo.get("geometry") or {}).get("type", "")
    if geom_type not in ("Polygon", "MultiPolygon"):
        raise HTTPException(status_code=400, detail=f"Geometri harus Polygon atau MultiPolygon, bukan {geom_type}.")

    if not geo.get("properties"):
        geo["properties"] = {}
    geo["properties"].setdefault("name", name)

    return {"feature": geo, "name": geo["properties"].get("name", name)}


@router.post("/convert_shp")
async def convert_shp(request: Request):
    zip_bytes: bytes | None = None
    original_name = "shapefile"
    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        f = form.get("file")
        if not f:
            raise HTTPException(status_code=400, detail="Field file tidak ditemukan.")
        original_name = (f.filename or "shapefile")[:255]
        zip_bytes = await f.read(_SHP_MAX_UPLOAD_BYTES + 1)
    else:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > SHP_JSON_BODY_LIMIT_BYTES:
            raise HTTPException(status_code=413, detail="Badan permintaan terlalu besar.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > SHP_JSON_BODY_LIMIT_BYTES:
                raise HTTPException(status_code=413, detail="Badan permintaan terlalu besar.")
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise HTTPException(status_code=400, detail="JSON permintaan tidak valid.") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Badan permintaan harus berupa objek JSON.")
        b64 = data.get("zip_b64") or ""
        if not b64:
            raise HTTPException(status_code=400, detail="zip_b64 wajib diisi.")
        submitted_name = data.get("name", "shapefile")
        original_name = submitted_name[:255] if isinstance(submitted_name, str) else "shapefile"
        try:
            zip_bytes = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="zip_b64 tidak valid base64.") from exc

    if zip_bytes is None or len(zip_bytes) > _SHP_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File terlalu besar (maks 50 MB).")
    try:
        with tempfile.TemporaryDirectory(prefix="savegeo-shapefile-") as directory:
            source = Path(directory) / "upload.zip"
            await run_in_threadpool(source.write_bytes, zip_bytes)
            result = await run_in_threadpool(
                run_isolated,
                "shapefile",
                {"source": str(source)},
                timeout=30,
                memory_mib=1024,
                max_output_bytes=16 * 1024 * 1024,
            )
    except ValueError as exc:
        message = str(exc)
        if "capacity is busy" in message:
            raise HTTPException(status_code=503, detail="Parser shapefile sedang sibuk. Coba lagi.") from exc
        if "time budget" in message or "resource budget" in message:
            raise HTTPException(status_code=413, detail="Shapefile melampaui batas pemrosesan.") from exc
        logger.warning("Isolated shapefile conversion rejected an upload")
        raise HTTPException(status_code=400, detail="ZIP shapefile tidak valid atau tidak dapat diproses.") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Isolated shapefile conversion failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Parser shapefile sementara tidak tersedia.") from exc
    result["name"] = result.get("name") or Path(original_name).stem or "shapefile"
    return result
