"""Refresh whole-Indonesia AOI from BIG ArcGIS REST service.

Source:
https://geoservices.big.go.id/gis/rest/services/BAPANAS/Batas_Administrasi/MapServer/0

The service exposes kabupaten/kota administrative polygons in EPSG:4326 and
supports GeoJSON output. The generated FeatureCollection keeps each official
feature separate; frontend and Earth Engine consumers in this app already
normalize FeatureCollections to one analysis geometry.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import requests

SOURCE_LAYER_URL = "https://geoservices.big.go.id/gis/rest/services/BAPANAS/Batas_Administrasi/MapServer/0"
OUT_PATH = Path(__file__).resolve().parents[1] / "app" / "static" / "geo" / "indonesia_whole.geojson"
MAX_ALLOWABLE_OFFSET = 0.005
OUT_FIELDS = ",".join(
    [
        "OBJECTID",
        "NAMOBJ",
        "KDPKAB",
        "KDPPUM",
        "WADMKK",
        "WADMPR",
        "LUASWH",
        "METADATA",
        "SRS_ID",
    ]
)


def _get_json(params: dict) -> dict:
    url = f"{SOURCE_LAYER_URL}/query?{urlencode(params)}"
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload


def _fetch_features() -> list[dict]:
    payload = _get_json(
        {
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "returnZ": "false",
            "returnM": "false",
            "geometryPrecision": 6,
            "maxAllowableOffset": MAX_ALLOWABLE_OFFSET,
        }
    )
    return payload.get("features", [])


def main() -> None:
    features = _fetch_features()
    if len(features) < 500:
        raise RuntimeError(f"Expected 500+ kabupaten/kota features from BIG, got {len(features)}")

    data = {
        "type": "FeatureCollection",
        "name": "indonesia_whole_big_kabkota",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "metadata": {
            "source_name": "Badan Informasi Geospasial (BIG) - Batas Administrasi Kabupaten/Kota",
            "source_url": SOURCE_LAYER_URL,
            "source_layer": "BAPANAS/Batas_Administrasi MapServer layer 0",
            "feature_count": len(features),
            "spatial_reference": "EPSG:4326 / OGC CRS84 coordinates",
            "download_params": {
                "f": "geojson",
                "outSR": 4326,
                "returnZ": False,
                "returnM": False,
                "geometryPrecision": 6,
                "maxAllowableOffset": MAX_ALLOWABLE_OFFSET,
            },
            "notes": (
                "AOI seluruh Indonesia dibentuk dari seluruh polygon kabupaten/kota resmi BIG. "
                "maxAllowableOffset dipakai agar file tetap praktis untuk web dan Earth Engine "
                "tanpa pergeseran bbox/cakupan."
            ),
        },
        "features": features,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(features)} features)", flush=True)


if __name__ == "__main__":
    main()
