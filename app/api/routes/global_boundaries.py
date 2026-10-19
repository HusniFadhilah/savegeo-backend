"""Cached access to geoBoundaries open country and ADM1/ADM2 boundaries."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException, Query
from shapely.geometry import shape as shapely_shape

from app.core.config import get_settings

router = APIRouter(prefix="/regions/global", tags=["regions"])
logger = logging.getLogger(__name__)

_API_ROOT = "https://www.geoboundaries.org/api/current/gbOpen"
_COUNTRY_ISO = re.compile(r"^[A-Z]{3}$")
_BOUNDARY_ID = re.compile(r"^[A-Z0-9-]{1,100}$")
_LEVELS = {"ADM0", "ADM1", "ADM2"}
_METADATA_TTL = 7 * 24 * 60 * 60
_GEOMETRY_TTL = 30 * 24 * 60 * 60
_METADATA_MAX_BYTES = 12 * 1024 * 1024
_GEOMETRY_MAX_BYTES = 128 * 1024 * 1024
_cache_lock = threading.RLock()


class BoundaryUnavailable(Exception):
    pass


class BoundaryNotFound(Exception):
    pass


def _cache_root() -> Path:
    settings = get_settings()
    configured = str(settings.global_boundary_cache_dir or "").strip()
    root = Path(configured) if configured else Path(settings.upload_dir).resolve().parent / "geoboundaries"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path(key: str) -> Path:
    # Keys are assembled only from constants and validated ISO3 country codes.
    return _cache_root() / f"{key}.json"


def _download_json(url: str, *, max_bytes: int) -> object:
    try:
        with requests.get(url, timeout=(10, 60), stream=True, headers={"User-Agent": "SAVEGEO/1.0 (geoBoundaries cache)"}) as response:
            if response.status_code == 404:
                raise BoundaryNotFound
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise BoundaryUnavailable("Upstream boundary file exceeds the configured size limit")
        return json.loads(body)
    except BoundaryNotFound:
        raise
    except (requests.RequestException, ValueError, BoundaryUnavailable) as exc:
        raise BoundaryUnavailable from exc


def _cached_json(key: str, url: str, *, ttl: int, max_bytes: int) -> object:
    path = _cache_path(key)
    with _cache_lock:
        try:
            age = time.time() - path.stat().st_mtime
            if age <= ttl:
                with path.open(encoding="utf-8") as stream:
                    return json.load(stream)
        except (OSError, ValueError):
            pass

        try:
            payload = _download_json(url, max_bytes=max_bytes)
        except BoundaryNotFound:
            raise
        except BoundaryUnavailable as upstream_error:
            try:
                with path.open(encoding="utf-8") as stream:
                    logger.warning("geoBoundaries unavailable; serving stale cache key=%s", key)
                    return json.load(stream)
            except (OSError, ValueError) as cache_error:
                raise upstream_error from cache_error

        temp_path = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)
        except OSError:
            logger.warning("Could not persist geoBoundaries cache key=%s", key)
        return payload


def _validate_iso3(iso3: str) -> str:
    value = iso3.upper()
    if not _COUNTRY_ISO.fullmatch(value):
        raise HTTPException(status_code=400, detail="iso3 must be a three-letter country code")
    return value


def _validate_level(level: str) -> str:
    value = level.upper()
    if value not in _LEVELS:
        raise HTTPException(status_code=400, detail="level must be ADM0, ADM1, or ADM2")
    return value


def _metadata(iso3: str, level: str) -> dict:
    payload = _cached_json(
        f"metadata_{iso3}_{level}",
        f"{_API_ROOT}/{iso3}/{level}/",
        ttl=_METADATA_TTL,
        max_bytes=_METADATA_MAX_BYTES,
    )
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict) or not payload.get("boundaryID"):
        raise BoundaryNotFound
    if not _BOUNDARY_ID.fullmatch(str(payload["boundaryID"])):
        raise BoundaryUnavailable("geoBoundaries returned an invalid boundary identifier")
    return payload


def _countries() -> list[dict]:
    payload = _cached_json(
        "metadata_ALL_ADM0",
        f"{_API_ROOT}/ALL/ADM0/",
        ttl=_METADATA_TTL,
        max_bytes=_METADATA_MAX_BYTES,
    )
    rows = payload if isinstance(payload, list) else [payload]
    return [
        row for row in rows
        if isinstance(row, dict) and _COUNTRY_ISO.fullmatch(str(row.get("boundaryISO", "")))
    ]


def _geometry_features(iso3: str, level: str) -> tuple[dict, list[dict]]:
    metadata = _metadata(iso3, level)
    geometry_url = metadata.get("simplifiedGeometryGeoJSON") or metadata.get("gjDownloadURL")
    parsed = urlparse(str(geometry_url or ""))
    if parsed.scheme != "https" or parsed.hostname != "github.com" or "/wmgeolab/geoBoundaries/raw/" not in parsed.path:
        raise BoundaryUnavailable("geoBoundaries metadata contains an unsupported geometry URL")

    payload = _cached_json(
        f"geometry_{iso3}_{level}_{metadata['boundaryID']}",
        str(geometry_url),
        ttl=_GEOMETRY_TTL,
        max_bytes=_GEOMETRY_MAX_BYTES,
    )
    if not isinstance(payload, dict):
        raise BoundaryNotFound
    if payload.get("type") == "FeatureCollection":
        features = payload.get("features") or []
    elif payload.get("type") == "Feature":
        features = [payload]
    else:
        features = [{"type": "Feature", "geometry": payload, "properties": {}}]
    features = [feature for feature in features if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)]
    if not features:
        raise BoundaryNotFound
    return metadata, features


def _boundary_name(properties: dict, metadata: dict, index: int, level: str) -> str:
    if level == "ADM0":
        return str(metadata.get("boundaryName") or properties.get("shapeName") or "")
    for key in ("shapeName", "name", "NAME", "NAME_1", "NAME_2", "admin", "nam"):
        value = properties.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return f"{metadata.get('boundaryType', level)} {index + 1}"


def _feature_options(metadata: dict, features: list[dict], level: str) -> list[dict]:
    options = []
    for index, feature in enumerate(features):
        properties = feature.get("properties") or {}
        options.append({
            "code": str(index),
            "name": _boundary_name(properties, metadata, index, level),
            "level": level,
            "year": str(metadata.get("boundaryYearRepresented") or ""),
            "source": str(metadata.get("boundarySource") or "geoBoundaries"),
            "license": str(metadata.get("boundaryLicense") or ""),
        })
    return sorted(options, key=lambda option: option["name"].casefold())


def _feature_belongs_to_parent(parent_geometry, child_geometry: dict) -> bool:
    try:
        child = shapely_shape(child_geometry)
        if parent_geometry.covers(child.representative_point()):
            return True
        return child.area > 0 and parent_geometry.intersection(child).area / child.area >= 0.5
    except Exception:  # noqa: BLE001 - malformed upstream topology should not break the list endpoint
        logger.warning("Could not compare geoBoundaries ADM2 polygon with its ADM1 parent")
        return False


@router.get("/countries")
def list_global_countries():
    try:
        return [
            {
                "code": row["boundaryISO"],
                "name": row.get("boundaryName") or row["boundaryISO"],
                "level": "ADM0",
                "year": str(row.get("boundaryYearRepresented") or ""),
                "source": str(row.get("boundarySource") or "geoBoundaries"),
                "license": str(row.get("boundaryLicense") or ""),
            }
            for row in sorted(_countries(), key=lambda item: str(item.get("boundaryName", "")).casefold())
        ]
    except BoundaryNotFound as exc:
        raise HTTPException(status_code=404, detail="Country boundaries are not available") from exc
    except BoundaryUnavailable as exc:
        raise HTTPException(status_code=502, detail="Global boundary provider temporarily unavailable") from exc


@router.get("/{iso3}/{level}")
def list_global_boundaries(
    iso3: str,
    level: str,
    parent_index: int | None = Query(default=None, ge=0, le=100000),
):
    iso3 = _validate_iso3(iso3)
    level = _validate_level(level)
    if level == "ADM0":
        raise HTTPException(status_code=400, detail="Use /countries for ADM0 boundaries")
    if level == "ADM2" and parent_index is None:
        raise HTTPException(status_code=400, detail="parent_index is required for ADM2 boundaries")
    try:
        metadata, features = _geometry_features(iso3, level)
        options = _feature_options(metadata, features, level)
        if level == "ADM2":
            _, parent_features = _geometry_features(iso3, "ADM1")
            if parent_index is None or parent_index >= len(parent_features):
                raise BoundaryNotFound
            try:
                parent_geometry = shapely_shape(parent_features[parent_index]["geometry"])
            except Exception as exc:  # noqa: BLE001 - do not expose broken upstream GeoJSON
                raise BoundaryUnavailable("Could not read parent boundary geometry") from exc
            # Feature options are sorted for the searchable UI. Their codes retain
            # the original geometry index, so the parent filter stays stable.
            options = [
                option for option in options
                if _feature_belongs_to_parent(parent_geometry, features[int(option["code"])]["geometry"])
            ]
            if not options:
                raise BoundaryNotFound
        return options
    except BoundaryNotFound as exc:
        raise HTTPException(status_code=404, detail=f"{level} boundaries are unavailable for {iso3}") from exc
    except BoundaryUnavailable as exc:
        raise HTTPException(status_code=502, detail="Global boundary provider temporarily unavailable") from exc
    except Exception as exc:  # noqa: BLE001 - upstream geometry must not produce an unhandled 500
        logger.exception("Could not prepare geoBoundaries boundary list")
        raise HTTPException(status_code=502, detail="Global boundary data could not be processed") from exc


@router.get("/{iso3}/{level}/geometry")
def get_global_boundary_geometry(
    iso3: str,
    level: str,
    index: int = Query(default=0, ge=0, le=100000),
):
    iso3 = _validate_iso3(iso3)
    level = _validate_level(level)
    try:
        metadata, features = _geometry_features(iso3, level)
        if level == "ADM0":
            selected = features
        else:
            if index >= len(features):
                raise BoundaryNotFound
            selected = [features[index]]

        annotated = []
        for feature in selected:
            properties = dict(feature.get("properties") or {})
            properties.update({
                "name": _boundary_name(properties, metadata, index, level),
                "code": str(properties.get("shapeID") or f"{iso3}-{level}-{index}"),
                "level": level,
                "country_iso": iso3,
                "boundary_source": metadata.get("boundarySource") or "geoBoundaries",
                "boundary_license": metadata.get("boundaryLicense") or "",
                "boundary_year": metadata.get("boundaryYearRepresented") or "",
                "boundary_attribution": "geoBoundaries (gbOpen)",
            })
            annotated.append({**feature, "properties": properties})

        if level == "ADM0":
            return {"type": "FeatureCollection", "features": annotated}
        return annotated[0]
    except BoundaryNotFound as exc:
        raise HTTPException(status_code=404, detail=f"{level} geometry is unavailable for {iso3}") from exc
    except BoundaryUnavailable as exc:
        raise HTTPException(status_code=502, detail="Global boundary provider temporarily unavailable") from exc
    except Exception as exc:  # noqa: BLE001 - upstream geometry must not produce an unhandled 500
        logger.exception("Could not prepare geoBoundaries geometry")
        raise HTTPException(status_code=502, detail="Global boundary data could not be processed") from exc
