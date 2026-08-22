"""Disaster monitoring service (BMKG alerts, DEM slope, before/after event mapping).

Ported (business logic unchanged) from legacy `backend/app.py`:
  - _parse_bmkg_warning_xml: lines ~853-889
  - get_disaster_sources / get_bmkg_alerts / get_disaster_dem_slope /
    get_disaster_event_map route bodies: lines ~2096-2345

Config values (BMKG_CAP_URL, INARISK_*, DEMNAS_*) come from `get_settings()`
instead of the legacy Flask `cfg()` helper.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

import ee
import requests

from app.core.config import get_settings
from app.services.gee_common import (
    AnalysisError,
    _date_or_default,
    _event_area_ha,
    _mask_s2_sr_clouds,
    create_geometry_from_payload,
    get_tile_url,
)

logger = logging.getLogger(__name__)


def _parse_bmkg_warning_xml(xml_text: str):
    root = ET.fromstring(xml_text)
    alerts = []

    def clean(value):
        return " ".join((value or "").split())

    if root.tag.endswith("rss") or root.find(".//channel") is not None:
        for item in root.findall(".//item"):
            alerts.append({
                "title": clean(item.findtext("title")),
                "description": clean(item.findtext("description")),
                "link": clean(item.findtext("link")),
                "published_at": clean(item.findtext("pubDate")),
                "source_format": "rss",
            })
        return alerts

    ns = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
    entries = root.findall(".//cap:alert", ns) or ([root] if root.tag.endswith("alert") else [])
    for alert in entries:
        info = alert.find("cap:info", ns)
        area = info.find("cap:area", ns) if info is not None else None
        alerts.append({
            "title": clean(info.findtext("cap:event", default="", namespaces=ns) if info is not None else ""),
            "description": clean(info.findtext("cap:description", default="", namespaces=ns) if info is not None else ""),
            "severity": clean(info.findtext("cap:severity", default="", namespaces=ns) if info is not None else ""),
            "urgency": clean(info.findtext("cap:urgency", default="", namespaces=ns) if info is not None else ""),
            "certainty": clean(info.findtext("cap:certainty", default="", namespaces=ns) if info is not None else ""),
            "area": clean(area.findtext("cap:areaDesc", default="", namespaces=ns) if area is not None else ""),
            "published_at": clean(alert.findtext("cap:sent", default="", namespaces=ns)),
            "source_format": "cap",
        })
    return alerts


def get_disaster_sources() -> dict:
    settings = get_settings()
    return {
        "inarisk": {
            "name": "BNPB InaRISK",
            "type": "WMS/XYZ/ArcGIS layer",
            "configured": bool((settings.inarisk_wms_url and settings.inarisk_wms_layers) or settings.inarisk_tile_url),
            "wms_url": settings.inarisk_wms_url,
            "layers": settings.inarisk_wms_layers,
            "tile_url": settings.inarisk_tile_url,
            "official_url": "https://inarisk.bnpb.go.id/",
            "note": "Isi INARISK_WMS_URL+INARISK_WMS_LAYERS atau INARISK_TILE_URL bila memakai service resmi/berizin BNPB.",
        },
        "bmkg": {
            "name": "BMKG Peringatan Dini Cuaca",
            "type": "CAP/XML feed",
            "configured": bool(settings.bmkg_cap_url),
            "feed_url": settings.bmkg_cap_url,
            "official_url": "https://data.bmkg.go.id/peringatan-dini-cuaca/",
        },
        "demnas": {
            "name": "BIG DEMNAS",
            "type": "WMS/XYZ/DEM asset",
            "configured": bool((settings.demnas_wms_url and settings.demnas_wms_layers) or settings.demnas_tile_url or settings.demnas_ee_asset),
            "wms_url": settings.demnas_wms_url,
            "layers": settings.demnas_wms_layers,
            "tile_url": settings.demnas_tile_url,
            "ee_asset": settings.demnas_ee_asset,
            "official_url": "https://www.big.go.id/content/product/demnas",
            "note": "Untuk slope berbasis DEMNAS, daftarkan DEMNAS_EE_ASSET atau service DEMNAS resmi di .env.",
        },
    }


def get_bmkg_alerts(limit: int = 30) -> dict:
    settings = get_settings()
    feed_url = settings.bmkg_cap_url
    if not feed_url:
        raise AnalysisError("BMKG_CAP_URL belum dikonfigurasi", 503)
    try:
        r = requests.get(feed_url, timeout=20, headers={"User-Agent": "SAVEGEO/1.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        raise AnalysisError(f"Gagal mengambil data BMKG: {e}", 502)

    try:
        alerts = _parse_bmkg_warning_xml(r.text)
    except ET.ParseError as e:
        raise AnalysisError(f"Gagal membaca XML BMKG: {e}", 502)

    return {
        "source": "BMKG",
        "feed_url": feed_url,
        "count": len(alerts),
        "alerts": alerts[:max(1, min(limit, 100))],
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def get_disaster_dem_slope(data: dict) -> dict:
    settings = get_settings()
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    try:
        aoi = create_geometry_from_payload(data["aoi"])
        demnas_asset = settings.demnas_ee_asset
        if demnas_asset:
            dem = ee.Image(demnas_asset).clip(aoi)
            source = "BIG DEMNAS (configured Earth Engine asset)"
            is_official_demnas = True
        else:
            dem = ee.Image("USGS/SRTMGL1_003").clip(aoi)
            source = "USGS SRTM fallback (not DEMNAS)"
            is_official_demnas = False

        slope = ee.Terrain.slope(dem).rename("slope").clip(aoi)
        slope_tile = get_tile_url(
            slope,
            {"min": 0, "max": 45, "palette": ["2e7d32", "fdd835", "fb8c00", "c62828"]},
            "Slope"
        )
        stats = slope.reduceRegion(
            reducer=ee.Reducer.mean().combine(ee.Reducer.max(), sharedInputs=True),
            geometry=aoi,
            scale=int(data.get("scale", 90)),
            maxPixels=int(settings.max_pixels),
            bestEffort=True,
        ).getInfo()
        return {
            "success": True,
            "source": source,
            "is_official_demnas": is_official_demnas,
            "tile_url": slope_tile["tile_url"] if slope_tile else None,
            "stats": {
                "mean_slope_deg": stats.get("slope_mean"),
                "max_slope_deg": stats.get("slope_max"),
            },
            "legend": [
                {"label": "0-10°", "color": "#2e7d32"},
                {"label": "10-20°", "color": "#fdd835"},
                {"label": "20-35°", "color": "#fb8c00"},
                {"label": ">35°", "color": "#c62828"},
            ],
        }
    except AnalysisError:
        raise
    except Exception as e:
        logger.exception("DEM slope analysis failed")
        raise AnalysisError(str(e), 500)


def get_disaster_event_map(data: dict) -> dict:
    if not data.get("aoi"):
        raise AnalysisError("aoi is required", 400)
    try:
        event_type = data.get("event_type", "flood")
        if event_type not in {"flood", "fire", "landslide"}:
            raise AnalysisError("event_type harus flood, fire, atau landslide", 400)

        today = datetime.now(UTC).date()
        after_end = _date_or_default(data.get("after_end"), today)
        after_start = _date_or_default(data.get("after_start"), today - timedelta(days=30))
        before_end = _date_or_default(data.get("before_end"), today - timedelta(days=31))
        before_start = _date_or_default(data.get("before_start"), today - timedelta(days=91))

        aoi = create_geometry_from_payload(data["aoi"])

        if event_type == "flood":
            scale = int(data.get("scale", 30))
            before_col = (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(aoi)
                .filterDate(before_start, before_end)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                .select("VV")
            )
            after_col = (
                ee.ImageCollection("COPERNICUS/S1_GRD")
                .filterBounds(aoi)
                .filterDate(after_start, after_end)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                .select("VV")
            )
            if before_col.size().getInfo() == 0 or after_col.size().getInfo() == 0:
                raise AnalysisError("Data Sentinel-1 sebelum/sesudah tidak cukup untuk AOI dan tanggal ini", 404)

            before = before_col.median().focal_median(30, "circle", "meters").clip(aoi)
            after = after_col.median().focal_median(30, "circle", "meters").clip(aoi)
            diff = after.subtract(before)
            event_mask = after.lt(float(data.get("water_threshold", -16))).And(diff.lt(float(data.get("change_threshold", -1.5)))).selfMask().rename("area")
            vis = {"palette": ["#1565c0"], "min": 1, "max": 1}
            title = "Area banjir terdeteksi"
            source = "Sentinel-1 SAR GRD (observasi sebelum/sesudah)"
            legend = [{"label": "Area tergenang terdeteksi", "color": "#1565c0"}]

        elif event_type == "fire":
            scale = int(data.get("scale", 20))
            before_col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(before_start, before_end)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
                .map(_mask_s2_sr_clouds)
            )
            after_col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(after_start, after_end)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
                .map(_mask_s2_sr_clouds)
            )
            if before_col.size().getInfo() == 0 or after_col.size().getInfo() == 0:
                raise AnalysisError("Data Sentinel-2 bebas awan sebelum/sesudah tidak cukup", 404)

            before = before_col.median().clip(aoi)
            after = after_col.median().clip(aoi)
            before_nbr = before.normalizedDifference(["B8", "B12"])
            after_nbr = after.normalizedDifference(["B8", "B12"])
            dnbr = before_nbr.subtract(after_nbr)
            event_mask = dnbr.gt(float(data.get("dnbr_threshold", 0.27))).selfMask().rename("area")
            vis = {"palette": ["#d7301f"], "min": 1, "max": 1}
            title = "Bekas kebakaran terdeteksi"
            source = "Sentinel-2 SR Harmonized dNBR (observasi sebelum/sesudah)"
            legend = [{"label": "Area terbakar terdeteksi", "color": "#d7301f"}]

        else:
            scale = int(data.get("scale", 20))
            before_col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(before_start, before_end)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
                .map(_mask_s2_sr_clouds)
            )
            after_col = (
                ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
                .filterBounds(aoi)
                .filterDate(after_start, after_end)
                .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
                .map(_mask_s2_sr_clouds)
            )
            if before_col.size().getInfo() == 0 or after_col.size().getInfo() == 0:
                raise AnalysisError("Data Sentinel-2 bebas awan sebelum/sesudah tidak cukup", 404)

            before = before_col.median().clip(aoi)
            after = after_col.median().clip(aoi)
            before_ndvi = before.normalizedDifference(["B8", "B4"])
            after_ndvi = after.normalizedDifference(["B8", "B4"])
            bsi = after.expression(
                "((swir + red) - (nir + blue)) / ((swir + red) + (nir + blue))",
                {
                    "swir": after.select("B11"),
                    "red": after.select("B4"),
                    "nir": after.select("B8"),
                    "blue": after.select("B2"),
                },
            )
            ndvi_drop = before_ndvi.subtract(after_ndvi)
            event_mask = ndvi_drop.gt(float(data.get("ndvi_drop_threshold", 0.25))).And(bsi.gt(float(data.get("bsi_threshold", 0.12)))).selfMask().rename("area")
            vis = {"palette": ["#8d6e63"], "min": 1, "max": 1}
            title = "Indikasi area longsor terdeteksi"
            source = "Sentinel-2 SR Harmonized NDVI/BSI change (observasi sebelum/sesudah)"
            legend = [{"label": "Indikasi longsor/perubahan lahan terbuka", "color": "#8d6e63"}]

        area_ha = _event_area_ha(event_mask, aoi, scale)
        tile = get_tile_url(event_mask, vis, title)
        return {
            "success": True,
            "event_type": event_type,
            "title": title,
            "source": source,
            "tile_url": tile["tile_url"] if tile else None,
            "area_ha": area_ha,
            "scale": scale,
            "before_period": {"start": before_start, "end": before_end},
            "after_period": {"start": after_start, "end": after_end},
            "legend": legend,
            "method_note": "Pemetaan berbasis perubahan observasi citra sebelum/sesudah kejadian, bukan prediksi risiko. Hasil tetap perlu validasi lapangan dan/atau laporan resmi kejadian.",
        }
    except AnalysisError:
        raise
    except ValueError as e:
        raise AnalysisError(str(e), 400)
    except Exception as e:
        logger.exception("Disaster event mapping failed")
        raise AnalysisError(str(e), 500)
