"""Explicit place searches and coarse imagery dates through a bounded proxy.

Nominatim has one application-wide request interval, including failures. Queries
are represented by hashes in the cache; no identity, original query or AOI is
stored. An unavailable cache/limiter fails closed instead of bypassing policy.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from threading import Lock

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.public_http import public_http
from app.db.models.provider_cache import ProviderCache
from app.db.session import SessionLocal

_local_lock = Lock()
_IMAGERY_COORDINATE_DECIMALS = 2
_IMAGERY_EXTENT_PADDING_DEGREES = 0.01


def cached_lookup(provider: str, params: dict, fetch, *, interval: float, ttl: float = 86400):
    key = hashlib.sha256(json.dumps([provider, params], sort_keys=True).encode()).hexdigest()
    policy_key = hashlib.sha256(f"policy:{provider}".encode()).hexdigest()
    db = SessionLocal()
    acquired = False
    try:
        if db.get_bind().dialect.name == "postgresql":
            lock_id = int.from_bytes(hashlib.sha256(provider.encode()).digest()[:7], "big")
            if not db.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}).scalar():
                raise HTTPException(429, "Layanan lokasi sedang sibuk. Coba lagi.", headers={"Retry-After": "2"})
        else:
            acquired = _local_lock.acquire(blocking=False)
            if not acquired:
                raise HTTPException(429, "Layanan lokasi sedang sibuk.", headers={"Retry-After": "2"})
        now = time.time()
        cached = db.get(ProviderCache, key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        policy = db.get(ProviderCache, policy_key)
        if policy is None:
            policy = ProviderCache(key=policy_key, provider=provider, expires_at=0, next_request_at=0)
            db.add(policy)
        if policy.next_request_at > now:
            raise HTTPException(429, "Tunggu sebentar sebelum mencari lokasi lain.",
                headers={"Retry-After": str(max(1, math.ceil(policy.next_request_at - now)))})
        policy.next_request_at = now + interval
        try:
            value = fetch()
        except Exception as exc:
            # Commit the request interval even when the upstream call failed.
            db.commit()
            raise HTTPException(502, "Layanan lokasi tidak tersedia. Coba lagi.") from exc
        if cached is None:
            cached = ProviderCache(key=key, provider=provider, next_request_at=0)
            db.add(cached)
        cached.value, cached.expires_at = value, now + ttl
        db.flush()
        db.query(ProviderCache).filter(ProviderCache.expires_at > 0, ProviderCache.expires_at < now).delete(synchronize_session=False)
        # Keep storage bounded even for many distinct (legitimate) searches.
        excess = db.query(ProviderCache.key).filter(ProviderCache.provider == provider, ProviderCache.expires_at > 0).order_by(ProviderCache.expires_at.desc()).offset(5000).limit(100).all()
        if excess:
            db.query(ProviderCache).filter(ProviderCache.key.in_([row[0] for row in excess])).delete(synchronize_session=False)
        db.commit()
        return value
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, "Cache layanan lokasi tidak tersedia.", headers={"Retry-After": "5"}) from exc
    finally:
        db.close()
        if acquired:
            _local_lock.release()


def search_places(q: str, *, limit: int, polygon: bool = False):
    params = {"q": q.strip(), "format": "json", "limit": limit, "addressdetails": 1}
    if polygon:
        params.update(polygon_geojson=1, polygon_threshold=0.0005, countrycodes="id")

    def fetch():
        response = public_http.get("https://nominatim.openstreetmap.org/search", params=params,
            headers={"User-Agent": "SaveGeo/1.0 (https://savegeo.len.co.id)"}, timeout=10, max_bytes=4 * 1024 * 1024)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("Invalid geocoder response")
        return data[:limit]

    return cached_lookup("nominatim", params, fetch, interval=1.1, ttl=300)


def imagery_date_metadata(lat: float, lng: float):
    # A 0.01-degree grid keeps the center to roughly kilometer precision.
    # The identify extent is also deliberately coarse. Exact cursor coordinates
    # and AOI polygons are not sent to Esri by this lookup.
    lat = round(lat, _IMAGERY_COORDINATE_DECIMALS)
    lng = round(lng, _IMAGERY_COORDINATE_DECIMALS)
    extent = _IMAGERY_EXTENT_PADDING_DEGREES
    params = {"f": "json", "geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint",
        "sr": "4326", "tolerance": "2", "mapExtent": f"{lng-extent},{lat-extent},{lng+extent},{lat+extent}",
        "imageDisplay": "400,400,96", "layers": "all", "returnGeometry": "false"}

    def fetch():
        response = public_http.get("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/identify",
            params=params, timeout=10, max_bytes=1024 * 1024)
        response.raise_for_status()
        data = response.json()
        # Only metadata needed by the date selector is returned or cached.
        keys = {"DATE (YYYYMMDD)", "MinMapLevel", "MaxMapLevel", "DrawOrder"}
        return {"results": [{"attributes": {k: v for k, v in item.get("attributes", {}).items() if k in keys}}
            for item in data.get("results", [])[:100] if isinstance(item, dict)]}

    result = cached_lookup("esri-imagery-date", params, fetch, interval=.2)
    return {
        **result,
        "data_sharing_notice": {
            "provider": "Esri World Imagery",
            "purpose": "look up imagery-date metadata for the map location",
            "coordinate_precision_degrees": 10 ** -_IMAGERY_COORDINATE_DECIMALS,
            "extent_padding_degrees": extent,
            "notice": {
                "id": "Pusat lokasi dibulatkan ke grid 0,01 derajat (kurang lebih 1 km). Titik dan cakupan pencarian sekitar 0,01 derajat di tiap sisi dikirim ke Esri untuk memperoleh metadata tanggal citra.",
                "en": "The map center is rounded to a 0.01-degree grid (roughly 1 km). The point and a search extent about 0.01 degrees on each side are sent to Esri to look up imagery-date metadata.",
            },
        },
    }
