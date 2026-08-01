"""Utility endpoints - /api/utils/*. Ported from backend/utils_routes.py."""
from __future__ import annotations

import base64
import io
import json
import logging
import zipfile
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/utils", tags=["utils"])
logger = logging.getLogger(__name__)

_UA = "GeoMoka/1.0 (geospatial platform; contact: admin@geomoka.id)"


@router.get("/geocode")
def geocode(q: str = ""):
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Parameter q wajib diisi.")

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 3, "addressdetails": 1},
            headers={"User-Agent": _UA},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json()
    except Exception as e:
        logger.warning("Nominatim error: %s", e)
        raise HTTPException(status_code=502, detail="Layanan geocoding tidak tersedia. Coba lagi.")

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


class ParseAoiRequest(BaseModel):
    text: str = ""
    name: Optional[str] = "Custom AOI"


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
    try:
        import shapefile  # pyshp
    except ImportError:
        raise HTTPException(status_code=500, detail="pyshp tidak terinstall. Jalankan: pip install pyshp")

    zip_bytes: Optional[bytes] = None
    original_name = "shapefile"
    content_type = request.headers.get("content-type", "")

    if "multipart" in content_type:
        form = await request.form()
        f = form.get("file")
        if not f:
            raise HTTPException(status_code=400, detail="Field file tidak ditemukan.")
        original_name = f.filename or "shapefile"
        zip_bytes = await f.read()
    else:
        data = await request.json()
        b64 = data.get("zip_b64") or ""
        if not b64:
            raise HTTPException(status_code=400, detail="zip_b64 wajib diisi.")
        original_name = data.get("name", "shapefile")
        try:
            zip_bytes = base64.b64decode(b64)
        except Exception:
            raise HTTPException(status_code=400, detail="zip_b64 tidak valid base64.")

    if len(zip_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File terlalu besar (maks 50 MB).")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Bukan file ZIP yang valid.")

    shp_names = [n for n in zf.namelist() if n.lower().endswith(".shp")]
    if not shp_names:
        raise HTTPException(status_code=400, detail="Tidak ada file .shp dalam ZIP.")

    shp_name = shp_names[0]
    base = shp_name[:-4]
    name_label = base.split("/")[-1] or original_name.replace(".zip", "")

    def _read(ext):
        try:
            return io.BytesIO(zf.read(base + ext))
        except KeyError:
            try:
                return io.BytesIO(zf.read(base + ext.upper()))
            except KeyError:
                return None

    shp_buf = _read(".shp")
    dbf_buf = _read(".dbf")
    shx_buf = _read(".shx")

    if not shp_buf:
        raise HTTPException(status_code=400, detail="Gagal membaca .shp dari ZIP.")

    try:
        kwargs = {"shp": shp_buf}
        if dbf_buf:
            kwargs["dbf"] = dbf_buf
        if shx_buf:
            kwargs["shx"] = shx_buf
        sf = shapefile.Reader(**kwargs)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal membaca shapefile: {e}")

    try:
        fields = [f[0] for f in sf.fields[1:]]
        features = []
        for sr in sf.shapeRecords():
            geom = sr.shape.__geo_interface__
            props = dict(zip(fields, sr.record))
            props = {k: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v) for k, v in props.items()}
            features.append({"type": "Feature", "geometry": geom, "properties": props})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal konversi ke GeoJSON: {e}")

    if not features:
        raise HTTPException(status_code=400, detail="Shapefile tidak memiliki feature.")

    fc = {"type": "FeatureCollection", "features": features}
    return {"geojson": fc, "feature_count": len(features), "name": name_label}
