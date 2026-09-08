"""Satellite catalog for the raw-imagery scene browser (/api/imagery/*).

Deliberately SEPARATE from satellite_provider_registry.py (the vegetation/
carbon index-analysis catalog): that registry assumes every entry has
optical red/green/blue/NIR/SWIR bands so NDVI-style index math
(calculate_index/standardize_bands) works the same way for all of them. SAR
(Sentinel-1: backscatter, no true color at all) and atmospheric-composition
sensors (Sentinel-5P: single-band gas concentration, no RGB either) don't fit
that model - mixing them into the vegetation catalog would be wrong. This
catalog instead tags each entry with a `visualization` strategy so
imagery_service.py's scene-tile builder knows how to render it:

  - "rgb": true-color composite from 3 named bands (Sentinel-2, Landsat 8/9,
    Sentinel-3 OLCI) - same idea as the vegetation catalog's RGB tiles.
  - "sar": Sentinel-1 backscatter (VV/VH, already in dB in this GEE
    collection) - grayscale VV by default, or a VV/VH/VV-VH false-color
    composite (see `sar_composite` below).
  - "single_band": one band + a color palette (Sentinel-5P gas products -
    NO2/CO/CH4/O3/aerosol index). Each gas is its own catalog entry since
    they're fundamentally different quantities with different natural
    ranges, not a single "pick a satellite" choice.

Band names, collection ids, and vis stretch ranges below were verified live
against the actual GEE collections (band names via `.bandNames().getInfo()`,
vis ranges via `.reduceRegion(percentile([2,50,98]))` over a real AOI) rather
than assumed - S5P's vis ranges in particular are close to (but adjusted
slightly from) Earth Engine's own published dataset-catalog defaults to
accommodate current (2023+) atmospheric baselines (e.g. CH4's background
concentration has risen past the older textbook 1750-1900 ppb range).
"""
from __future__ import annotations

from typing import Any

# Standard "rainbow" colormap used across Google's own Sentinel-5P dataset
# catalog pages (black=low -> red=high) - kept identical here for
# familiarity to anyone who's seen these products visualized elsewhere.
_GAS_PALETTE = ["black", "blue", "purple", "cyan", "green", "yellow", "red"]

IMAGERY_PROVIDERS: dict[str, dict[str, Any]] = {
    "big_ctsrt": {
        "key": "big_ctsrt",
        "name": "BIG / CTSRT - Citra Satelit Resolusi Tinggi",
        "provider": "Badan Informasi Geospasial (BIG)",
        "group": "BIG / CTSRT",
        "gee_collection": "",
        "source_kind": "big_ctsrt",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 0.5,
        "revisit_days": 0,
        "start_year": 2014,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "description": "Katalog mosaic Citra Tegak Satelit Resolusi Tinggi (CTSRT) BIG per wilayah/tahun. Citra dirender langsung dari layanan publik ArcGIS ImageServer BIG.",
    },
    "stac_catalog": {
        "key": "stac_catalog",
        "name": "Generic STAC Catalog Browser",
        "provider": "Custom STAC / COG",
        "group": "Custom STAC",
        "gee_collection": "",
        "source_kind": "generic_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 1,
        "revisit_days": 0,
        "start_year": 2010,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "description": "Browser katalog STAC publik/custom. Masukkan URL Catalog/API STAC; scene dengan asset GeoTIFF/COG bisa dirender, diunduh, dan dizoom lewat footprint.",
    },
    "planet_open_data": {
        "key": "planet_open_data",
        "name": "Planet Open Data - Disaster Imagery",
        "provider": "Planet / Source Cooperative",
        "group": "Open Disaster",
        "gee_collection": "",
        "source_kind": "planet_open_data_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 3,
        "revisit_days": 0,
        "start_year": 2015,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "description": "Katalog open-data Planet untuk event bencana/kemanusiaan di Source Cooperative. Gratis untuk event tertentu, bukan arsip global seluruh lokasi/tanggal.",
    },
    "openaerialmap": {
        "key": "openaerialmap",
        "name": "OpenAerialMap - Open UAV/Aerial Imagery",
        "provider": "HOT / OpenAerialMap",
        "group": "Open Aerial",
        "gee_collection": "",
        "source_kind": "oam_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 0.05,
        "revisit_days": 0,
        "start_year": 2015,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "description": "Katalog imagery terbuka berbasis STAC: UAV, aerial, dan beberapa citra satelit open-data. Resolusi bisa centimeter, tetapi cakupan dan tanggal sangat bergantung kontribusi komunitas/event.",
    },
    "vantor_open_data": {
        "key": "vantor_open_data",
        "name": "Vantor/Maxar Open Data - Disaster Imagery",
        "provider": "Vantor / Maxar Open Data Program",
        "group": "Open Disaster",
        "gee_collection": "",
        "source_kind": "maxar_open_data_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 0.3,
        "revisit_days": 0,
        "start_year": 2015,
        "cloud_property": "tile:clouds_percent",
        "cloud_mask_techniques": None,
        "description": "Citra satelit resolusi tinggi untuk rilis bencana/kemanusiaan publik. Gratis untuk open-data event tertentu, bukan katalog global semua tanggal/lokasi; lisensi CC-BY-NC-4.0.",
    },
    "sentinel2": {
        "key": "sentinel2",
        "name": "Sentinel-2 - Surface Reflectance (L2A)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-2",
        "gee_collection": "COPERNICUS/S2_SR_HARMONIZED",
        "visualization": "rgb",
        "color_mode": "natural",
        "band_role_map": {"red": "B4", "green": "B3", "blue": "B2"},
        "reflectance_scale": 0.0001,  # divide by 10000
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUDY_PIXEL_PERCENTAGE",
        # SCL (Scene Classification Layer) is an L2A-only product - not
        # present on the L1C/TOA variant below. QA60 and s2cloudless both
        # work on either (verified live: same system:index granule scheme,
        # same s2cloudless join key, across L1C and L2A).
        "cloud_mask_techniques": ["scl", "qa60", "s2cloudless"],
        "resolution_m": 10, "revisit_days": 5, "start_year": 2017,
        "description": "Optik resolusi tertinggi di sini (10m) - true-color RGB, sudah dikoreksi atmosfer (siap pakai untuk analisis). Data L2A baru tersedia sejak 2017 (lihat varian TOA di bawah untuk 2015-2016).",
    },
    "sentinel2_l1c": {
        "key": "sentinel2_l1c",
        "name": "Sentinel-2 - TOA (L1C)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-2",
        "gee_collection": "COPERNICUS/S2_HARMONIZED",
        "visualization": "rgb",
        "color_mode": "natural",
        "band_role_map": {"red": "B4", "green": "B3", "blue": "B2"},
        "reflectance_scale": 0.0001,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUDY_PIXEL_PERCENTAGE",
        # No SCL band on L1C (verified live: bandNames has QA60/MSK_CLASSI_*
        # but no SCL) - only QA60/s2cloudless apply here.
        "cloud_mask_techniques": ["qa60", "s2cloudless"],
        "resolution_m": 10, "revisit_days": 5, "start_year": 2015,
        "description": "Top-of-Atmosphere (belum dikoreksi atmosfer) - satu-satunya varian Sentinel-2 yang mencakup 2015-2016 (verified live: data nyata ditemukan sejak akhir 2015), sebelum pemrosesan Surface Reflectance (L2A) tersedia secara global mulai 2017. Warna bisa sedikit lebih pudar/berkabut dibanding L2A karena belum ada koreksi atmosfer.",
    },
    "landsat8": {
        "key": "landsat8",
        "name": "Landsat 8 (OLI/TIRS)",
        "provider": "USGS / NASA",
        "group": "Landsat",
        "gee_collection": "LANDSAT/LC08/C02/T1_L2",
        "visualization": "rgb",
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B4", "green": "SR_B3", "blue": "SR_B2"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 2013,
        "description": "Optik 30m - arsip historis terpanjang di sini (sejak 2013).",
    },
    "landsat9": {
        "key": "landsat9",
        "name": "Landsat 9 (OLI-2/TIRS-2)",
        "provider": "USGS / NASA",
        "group": "Landsat",
        "gee_collection": "LANDSAT/LC09/C02/T1_L2",
        "visualization": "rgb",
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B4", "green": "SR_B3", "blue": "SR_B2"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 2021,
        "description": "Optik 30m - penerus Landsat 8, sejak 2021.",
    },
    "sentinel1": {
        "key": "sentinel1",
        "name": "Sentinel-1 (SAR)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-1",
        "gee_collection": "COPERNICUS/S1_GRD",
        "visualization": "sar",
        # Verified live: bands are ['VV','VH','angle'], already in dB (not
        # linear power) - typical land backscatter here measured ~-20 to
        # -12 dB (VV) / -29 to -22 dB (VH) via real percentile check.
        "sar_bands": {"vv": "VV", "vh": "VH"},
        "sar_grayscale_vis": {"band": "VV", "min": -25, "max": 0},
        # False-color composite (R=VV, G=VH, B=VV-VH difference in dB) - a
        # widely used Sentinel-1 visualization convention, not something
        # invented here.
        "sar_composite_vis": {"min": [-20, -25, -5], "max": [0, -5, 15]},
        "cloud_property": None,  # radar - unaffected by cloud cover entirely
        "cloud_mask_techniques": None,
        "resolution_m": 10, "revisit_days": 6, "start_year": 2014,
        "description": "Radar C-band - tembus awan & bekerja malam hari. Tidak ada foto RGB asli (bukan sensor optik); ditampilkan grayscale VV atau komposit false-color.",
    },
    "sentinel3": {
        "key": "sentinel3",
        "name": "Sentinel-3 (OLCI)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-3",
        "gee_collection": "COPERNICUS/S3/OLCI",
        "visualization": "rgb",
        "color_mode": "natural",
        # Verified live: Level-1B TOA radiance (not 0-1 reflectance like S2/
        # Landsat) - real percentile check over a land AOI gave ~80-490 for
        # the RGB-ish bands, hence the very different vis_min/max below.
        "band_role_map": {"red": "Oa08_radiance", "green": "Oa06_radiance", "blue": "Oa04_radiance"},
        "vis_min": 0, "vis_max": 550,
        "cloud_property": None,  # no scene-level cloud % property on this collection
        "cloud_mask_techniques": None,
        "resolution_m": 300, "revisit_days": 1, "start_year": 2016,
        "description": "Optik 300m - cakupan sangat luas, revisit ~harian. Resolusi jauh lebih kasar dari Sentinel-2/Landsat.",
    },
    "sentinel5p_no2": {
        "key": "sentinel5p_no2",
        "name": "Sentinel-5P - NO2 (Nitrogen Dioksida)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-5P",
        "gee_collection": "COPERNICUS/S5P/OFFL/L3_NO2",
        "visualization": "single_band",
        "band": "tropospheric_NO2_column_number_density",
        "palette": _GAS_PALETTE, "vis_min": 0, "vis_max": 0.0002,
        "unit": "mol/m²",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 1113, "revisit_days": 1, "start_year": 2018,
        "description": "Bukan RGB - peta konsentrasi gas NO2 troposfer (indikator polusi udara/lalu lintas/industri), satu-band dengan gradasi warna.",
    },
    "sentinel5p_co": {
        "key": "sentinel5p_co",
        "name": "Sentinel-5P - CO (Karbon Monoksida)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-5P",
        "gee_collection": "COPERNICUS/S5P/OFFL/L3_CO",
        "visualization": "single_band",
        "band": "CO_column_number_density",
        "palette": _GAS_PALETTE, "vis_min": 0.02, "vis_max": 0.05,
        "unit": "mol/m²",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 1113, "revisit_days": 1, "start_year": 2018,
        "description": "Bukan RGB - konsentrasi karbon monoksida (indikator kebakaran/pembakaran biomassa/emisi kendaraan).",
    },
    "sentinel5p_ch4": {
        "key": "sentinel5p_ch4",
        "name": "Sentinel-5P - CH4 (Metana)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-5P",
        "gee_collection": "COPERNICUS/S5P/OFFL/L3_CH4",
        "visualization": "single_band",
        "band": "CH4_column_volume_mixing_ratio_dry_air_bias_corrected",
        "palette": _GAS_PALETTE, "vis_min": 1800, "vis_max": 1950,
        "unit": "ppb",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 1113, "revisit_days": 1, "start_year": 2018,
        "description": "Bukan RGB - konsentrasi metana atmosfer (indikator emisi lahan gambut/sampah/pertanian/gas alam).",
    },
    "sentinel5p_o3": {
        "key": "sentinel5p_o3",
        "name": "Sentinel-5P - O3 (Ozon)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-5P",
        "gee_collection": "COPERNICUS/S5P/OFFL/L3_O3",
        "visualization": "single_band",
        "band": "O3_column_number_density",
        "palette": _GAS_PALETTE, "vis_min": 0.10, "vis_max": 0.15,
        "unit": "mol/m²",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 1113, "revisit_days": 1, "start_year": 2018,
        "description": "Bukan RGB - kolom ozon total atmosfer.",
    },
    "sentinel5p_aerosol": {
        "key": "sentinel5p_aerosol",
        "name": "Sentinel-5P - Indeks Aerosol",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-5P",
        "gee_collection": "COPERNICUS/S5P/OFFL/L3_AER_AI",
        "visualization": "single_band",
        "band": "absorbing_aerosol_index",
        "palette": _GAS_PALETTE, "vis_min": -1, "vis_max": 2,
        "unit": "indeks (tanpa satuan)",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 1113, "revisit_days": 1, "start_year": 2018,
        "description": "Bukan RGB - indeks aerosol penyerap (asap kebakaran hutan, debu) - positif menandakan aerosol menyerap radiasi UV (asap/debu), negatif = udara bersih/awan.",
    },
    "aster": {
        "key": "aster",
        "name": "ASTER (Terra)",
        "provider": "NASA / METI (Jepang)",
        "group": "ASTER",
        "gee_collection": "ASTER/AST_L1T_003",
        "visualization": "rgb",
        # ASTER's VNIR subsystem has NO blue band (only green/red/NIR) - so
        # unlike every other "rgb" entry here, this can only ever be a
        # false-color composite (NIR as red channel is the standard ASTER
        # convention), never true natural color. `color_mode` lets the
        # frontend show the same "bukan foto natural" disclosure it already
        # shows for SAR/gas products, instead of silently implying this looks
        # like a normal photo.
        "color_mode": "false_color",
        "band_role_map": {"red": "B3N", "green": "B02", "blue": "B01"},
        # Verified live: AST_L1T is terrain-corrected radiance already scaled
        # to 8-bit DN (0-255), not raw radiance or 0-1 reflectance like the
        # other optical entries - no reflectance_scale/offset needed.
        "vis_min": 0, "vis_max": 255,
        "cloud_property": "CLOUDCOVER",
        "cloud_mask_techniques": None,
        "resolution_m": 15, "revisit_days": 16, "start_year": 2000,
        "description": "Resolusi 15m (di bawah Landsat, di atas Sentinel-2 dalam hal usia arsip - sejak 2000). Tidak ada band biru asli - ditampilkan false-color (NIR-Merah-Hijau). Akuisisi berdasarkan permintaan/tasking, bukan jadwal tetap - cakupan per lokasi tidak serapat Sentinel-2/Landsat.",
    },
    "viirs_dnb": {
        "key": "viirs_dnb",
        "name": "VIIRS - Citra Lampu Malam (Night Lights)",
        "provider": "NASA / NOAA",
        "group": "VIIRS",
        "gee_collection": "NOAA/VIIRS/001/VNP46A2",
        "visualization": "single_band",
        # Gap-filled, BRDF/lunar-corrected nighttime radiance composite -
        # verified live: real values for a populated coastal AOI ran ~6-180
        # (median ~28) nW/cm²/sr, matching typical VIIRS DNB city-light ranges.
        "band": "Gap_Filled_DNB_BRDF_Corrected_NTL",
        "palette": ["000000", "1a1a2e", "16213e", "e94560", "f9c74f", "ffffff"],
        "vis_min": 0, "vis_max": 60,
        "unit": "nW/cm²/sr",
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 500, "revisit_days": 1, "start_year": 2012,
        "description": "Bukan foto siang hari - radiance cahaya malam (lampu kota, kapal/alat tangkap ikan lepas pantai, area industri). Berguna untuk memantau pertumbuhan area terbangun atau kemungkinan aktivitas malam hari yang tidak biasa. Produk komposit HARIAN (per tanggal kalender) - jam yang tertera bukan waktu perekaman presisi seperti sensor lain.",
    },
}

DEFAULT_IMAGERY_PROVIDER = "sentinel2"


def resolve_imagery_provider(key: str | None) -> str:
    return key if key in IMAGERY_PROVIDERS else DEFAULT_IMAGERY_PROVIDER


def get_imagery_provider_meta(key: str | None) -> dict[str, Any]:
    return IMAGERY_PROVIDERS[resolve_imagery_provider(key)]
