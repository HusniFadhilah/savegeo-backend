"""Explicit field-factor stock scenarios and observed MODIS productivity."""

import math
from datetime import UTC, datetime

import ee

from app.services.gee_common import AnalysisError, create_geometry_from_payload
from app.services.carbon_pool_service import _pool_density_statistics, summary_scale

MODIS = "MODIS/061/MOD17A3HGF"
MODIS_URL = "https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD17A3HGF"
PALETTE = ["f7fcb9", "addd8e", "31a354", "006837"]
IPCC_URL = "https://www.ipcc-nggip.iges.or.jp/public/2019rf/pdf/4_Volume4/19R_V4_Ch02_Generic%20Methods.pdf"
# IPCC 2019 Refinement, Volume 4 Chapter 2, Table 2.2, all vegetation types.
DETRITUS_PRESETS = {
    "tropical_rainforest": {"label": "Hutan hujan tropis", "litter": 4.8, "deadwood": 14.8},
    "tropical_moist": {"label": "Hutan tropis lembap", "litter": 5.9, "deadwood": 8.0},
    "tropical_dry": {"label": "Hutan tropis kering", "litter": 2.4, "deadwood": 9.0},
}


def required_number(settings, key, minimum=0, maximum=100000):
    value = settings.get(key)
    if value is None or isinstance(value, bool):
        raise AnalysisError(f"Nilai {key} wajib diisi dari data pengukuran/literatur.", 422)
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise AnalysisError(f"Nilai {key} tidak valid.", 422) from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AnalysisError(f"Nilai {key} harus antara {minimum} dan {maximum}.", 422)
    return value


def annual_labels(roi, year):
    collection = (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterBounds(roi)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
    )
    if not collection.size().getInfo():
        raise AnalysisError(f"Dynamic World tahun {year} belum tersedia pada wilayah ini.", 404)
    return collection.select("label").mode()


def productivity_years():
    values = ee.ImageCollection(MODIS).aggregate_array("system:time_start").getInfo() or []
    return sorted({datetime.fromtimestamp(value / 1000, UTC).year for value in values}, reverse=True)


def build_extension_image(data, roi):
    """Return raster plus provenance; this builder is also used by GeoTIFF exports."""
    kind = data.get("extension")
    if not isinstance(kind, str):
        raise AnalysisError("Jenis modul tambahan tidak valid.", 422)
    settings = data.get("extension_settings") or {}
    if not isinstance(settings, dict):
        raise AnalysisError("Parameter modul tambahan harus berupa objek.", 422)
    settings = dict(settings)
    year = int(data.get("year", datetime.now(UTC).year))
    if year < 2001 or year > datetime.now(UTC).year:
        raise AnalysisError("Tahun modul karbon harus antara 2001 dan tahun berjalan.", 422)
    if kind in {"npp", "gpp"}:
        images = ee.ImageCollection(MODIS).filterBounds(roi).filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        if not images.size().getInfo():
            latest = ee.ImageCollection(MODIS).aggregate_max("system:time_start").getInfo()
            latest_year = datetime.fromtimestamp(latest / 1000, UTC).year if latest else "tidak diketahui"
            raise AnalysisError(
                f"MODIS produktivitas tahunan {year} belum tersedia. Tahun terakhir: {latest_year}. Pilih tahun sumber yang tersedia.",
                404,
            )
        source = ee.Image(images.first())
        band = "Npp" if kind == "npp" else "Gpp"
        raw = source.select(band)
        valid = raw.gte(-30000 if kind == "npp" else 0).And(raw.lte(32700 if kind == "npp" else 65500))
        # kg C/m2 * 0.0001 * 10 = Mg C/ha. NPP can legitimately be negative.
        converted = raw.updateMask(valid).multiply(0.001).rename("agb")
        # MODIS' global sinusoidal footprint contains edges that cannot be
        # transformed by clip(). A single-image mosaic removes that global
        # footprint constraint without changing the selected year or values.
        image = ee.ImageCollection.fromImages([converted]).mosaic()
        return image, {
            "name": f"MODIS {kind.upper()} tahunan",
            "source": MODIS_URL,
            "year": year,
            "resolution": 500,
            "unit": "Mg C/ha/tahun",
            "quantity": "productivity",
            "description": "Produktivitas vegetasi tahunan MOD17A3HGF. NPP mencakup respirasi autotrof; bukan serapan bersih ekosistem, bukan pengukuran daun individual, dan bukan kredit karbon. Tidak dilakukan proyeksi ke tahun yang belum tersedia.",
        }
    if kind not in {"litter", "deadwood", "bluecarbon"}:
        raise AnalysisError("Modul karbon tambahan tidak dikenali.", 422)
    preset = None
    if kind in {"litter", "deadwood"} and settings.get("preset"):
        preset = DETRITUS_PRESETS.get(str(settings["preset"]))
        if not preset or settings.get("domain_confirmed") is not True:
            raise AnalysisError(
                "Pilih preset iklim hutan yang sesuai dan konfirmasikan domain penerapannya.", 422
            )
        settings.update(
            density=preset[kind],
            mask="trees",
            source=f"IPCC 2019 Refinement Vol.4 Ch.2 Table 2.2; {preset['label']}; all vegetation types; {IPCC_URL}",
        )
    source = str(settings.get("source") or "").strip()
    if len(source) < 5 or len(source) > 2000:
        raise AnalysisError("Cantumkan sumber koefisien/pengukuran (5-2000 karakter).", 422)
    if kind == "bluecarbon":
        settings.update(
            {key: required_number(settings, key) for key in ("agb_density", "bgb_density", "soc_density")}
        )
        density = sum(settings[key] for key in ("agb_density", "bgb_density", "soc_density"))
        depth = required_number(settings, "soil_depth_cm", 1, 300)
        settings["soil_depth_cm"] = depth
        habitat = settings.get("habitat", "mangrove_2000")
        if habitat == "mangrove_2000":
            mask = ee.ImageCollection("LANDSAT/MANGROVE_FORESTS").mosaic().select("1").eq(1).selfMask()
            habitat_note = "Mask mangrove Landsat tahun 2000; bukan sebaran mangrove tahun analisis."
        elif habitat == "aoi_habitat":
            if settings.get("habitat_confirmed") is not True:
                raise AnalysisError(
                    "Konfirmasi bahwa AOI hanya berisi habitat mangrove/lamun/rawa asin terpetakan.", 422
                )
            mask = ee.Image.constant(1).clip(roi)
            habitat_note = (
                f"Batas habitat dari AOI pengguna ({settings.get('habitat_type', 'habitat pesisir')})."
            )
        else:
            raise AnalysisError("Pilihan habitat blue carbon tidak valid.", 422)
        description = f"Skenario stok AGB+BGB+SOC menggunakan koefisien Mg C/ha dari {source}; kedalaman sedimen {depth:g} cm. {habitat_note} Tidak mencakup emisi, laju penimbunan, perubahan stok, atau verifikasi kredit."
    else:
        density = required_number(settings, "density")
        settings["density"] = density
        if settings.get("mask", "trees") not in {"trees", "aoi"}:
            raise AnalysisError("Mask stok serasah/kayu mati harus trees atau aoi.", 422)
        mask = (
            annual_labels(roi, year).eq(1).selfMask()
            if settings.get("mask", "trees") == "trees"
            else ee.Image.constant(1).clip(roi)
        )
        description = f"Skenario stok {kind} dengan koefisien {density:g} Mg C/ha dari {source}. Diterapkan pada {'kelas pohon Dynamic World tahunan' if settings.get('mask', 'trees') == 'trees' else 'seluruh AOI'}. Bukan inferensi stok terkalibrasi dari citra."
        if preset:
            description += " Nilai Tier 1 merupakan default iklim hutan berskala luas dengan ketidakpastian tinggi; bukan hasil survei lokal atau estimasi perubahan stok tahunan."
    return ee.Image.constant(density).rename("agb").updateMask(mask).clip(roi), {
        "name": {
            "litter": "Karbon serasah",
            "deadwood": "Karbon kayu mati",
            "bluecarbon": "Blue carbon - skenario habitat",
        }[kind],
        "source": source,
        "year": year,
        "reference_year": 2019 if preset else year,
        "settings": settings,
        "resolution": 30 if kind == "bluecarbon" else 10,
        "unit": "Mg C/ha",
        "quantity": "stock_scenario",
        "description": description,
    }


def analyze_extension(data):
    roi = create_geometry_from_payload(data["aoi"])
    image, meta = build_extension_image(data, roi)
    scale = summary_scale(meta["resolution"], roi.bounds(maxError=100).area(maxError=100).getInfo(), 250000)
    image = image.clip(roi)
    area = ee.Image.pixelArea().divide(10000).updateMask(image.mask()).rename("valid_area")
    bands = image.addBands(area).addBands(image.multiply(area).rename("carbon_total"))
    reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.stdDev(), sharedInputs=True)
        .combine(ee.Reducer.minMax(), sharedInputs=True)
        .combine(ee.Reducer.sum(), sharedInputs=True)
        .combine(ee.Reducer.percentile([25, 50, 75]), sharedInputs=True)
        .combine(ee.Reducer.histogram(32, None, 10000), sharedInputs=True)
    )
    raw = bands.reduceRegion(
        reducer=reducer, geometry=roi, scale=scale, maxPixels=1000000, tileScale=4
    ).getInfo()
    if raw.get("agb_mean") is None or not raw.get("valid_area_sum"):
        raise AnalysisError("Tidak ada piksel habitat/vegetasi valid pada AOI dan periode ini.", 422)
    stats = _pool_density_statistics(raw, scale, meta["resolution"])
    vis = {"min": raw["agb_min"], "max": max(raw["agb_min"] + 0.01, raw["agb_max"]), "palette": PALETTE}
    tile = image.getMapId(vis)["tile_fetcher"].url_format
    return {
        "carbon_estimated": {
            **(
                {"depth_cm": [0, meta["settings"]["soil_depth_cm"]]}
                if data["extension"] == "bluecarbon"
                else {}
            ),
            "tile_url": tile,
            "statistics": stats,
            "unit": meta["unit"],
            "quantity": meta["quantity"],
            "vis_params": vis,
            "description": meta["description"],
        },
        "carbon_reference": {
            "name": meta["name"],
            "year": meta.get("reference_year", meta["year"]),
            "resolution": meta["resolution"],
            "description": meta["description"],
        },
        "area_info": {
            "calculation_area_ha": raw["valid_area_sum"],
            "area_ha": raw["valid_area_sum"],
            "total_carbon_tons": raw["carbon_total_sum"],
            "carbon_dioxide_equivalent_tons": None,
        },
        "model_info": {
            **(
                {"depth_cm": [0, meta["settings"]["soil_depth_cm"]]}
                if data["extension"] == "bluecarbon"
                else {}
            ),
            **(
                {
                    "source_period": f"{meta['year']}-01-01 / {meta['year']}-12-31",
                    "observed_through": f"{meta['year']}-12-31",
                }
                if data["extension"] in {"npp", "gpp"}
                else {}
            ),
            "analysis_year": meta["year"],
            "reference_dataset": meta["name"],
            "reference_dataset_year": meta.get("reference_year", meta["year"]),
            "scale": scale,
            "calculation_mode": "clipped_aoi",
            "statistics_available": True,
            "method_description": meta["description"],
            "validation_note": "Akurasi lokal memerlukan pengukuran independen. Skenario tidak memiliki metrik validasi model.",
            "source_url": meta["source"],
            "extension_payload": {
                "extension": data["extension"],
                "extension_settings": meta.get("settings", data.get("extension_settings", {})),
            },
        },
    }
