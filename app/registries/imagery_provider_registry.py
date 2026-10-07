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

  - "rgb": true-color composite from 3 named bands (Sentinel-2, Landsat 4/5/7/8/9,
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
    "himawari": {
        "key": "himawari",
        "name": "Himawari-9 - Cuaca Asia-Pasifik",
        "provider": "Japan Meteorological Agency (JMA)",
        "group": "Cuaca Geostasioner",
        "gee_collection": "",
        "source_kind": "himawari",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": 2000,
        "revisit_days": 0.007,
        "start_year": 2015,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "description": "Citra cuaca full-disk Asia-Pasifik berbasis frame JMA. Pilih waktu UTC untuk melihat awan dan sistem cuaca; resolusi kilometer dan bukan scene detail untuk analisis tutupan lahan.",
    },
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
    "planet_commercial": {
        "key": "planet_commercial",
        "name": "Planet commercial imagery",
        "provider": "Planet",
        "group": "Commercial EO",
        "gee_collection": "",
        "source_kind": "planet_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": None,
        "revisit_days": None,
        "start_year": None,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "license": "Planet customer license",
        "description": "Katalog Planet berlisensi melalui konektor STAC backend. Dukungan STAC Planet saat ini beta; produk, cakupan, resolusi, asset, dan hak akses mengikuti koleksi serta entitlement akun yang dikonfigurasi. Search/preview tidak membuat order atau tasking.",
        "capabilities": {
            "searchable": True, "downloadable": True, "supports_preview": True,
            "supports_compare": True, "supports_mosaic": False,
            "supports_bands": True, "supports_raw_data": True, "supports_ai": False,
            "visualization_only": True, "analytical": False,
            "preview_kind": "cog", "supports_visual_enhancement": False,
        },
    },
    "vantor_commercial": {
        "key": "vantor_commercial",
        "name": "Vantor/Maxar commercial imagery",
        "provider": "Vantor / Maxar",
        "group": "Commercial EO",
        "gee_collection": "",
        "source_kind": "vantor_stac",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": None,
        "revisit_days": None,
        "start_year": None,
        "cloud_property": "eo:cloud_cover",
        "cloud_mask_techniques": None,
        "license": "Vantor/Maxar customer license",
        "description": "Katalog Vantor/Maxar berlisensi melalui konektor STAC backend. Produk, cakupan, resolusi, asset, dan hak akses mengikuti koleksi serta entitlement akun yang dikonfigurasi.",
        "capabilities": {
            "searchable": True, "downloadable": True, "supports_preview": True,
            "supports_compare": True, "supports_mosaic": False,
            "supports_bands": True, "supports_raw_data": True, "supports_ai": False,
            "visualization_only": True, "analytical": False,
            "preview_kind": "cog", "supports_visual_enhancement": False,
        },
    },
    "iceye_commercial": {
        "key": "iceye_commercial",
        "name": "ICEYE commercial SAR",
        "provider": "ICEYE",
        "group": "Commercial SAR",
        "gee_collection": "",
        "source_kind": "iceye_stac",
        "visualization": "sar",
        "resolution_m": None,
        "revisit_days": None,
        "start_year": None,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "license": "ICEYE customer license",
        "description": "Katalog ICEYE SAR berlisensi melalui konektor STAC backend. Produk, cakupan, resolusi, asset, dan hak akses mengikuti koleksi serta entitlement akun yang dikonfigurasi.",
        "capabilities": {
            "searchable": True, "downloadable": True, "supports_preview": True,
            "supports_compare": True, "supports_mosaic": False,
            "supports_bands": True, "supports_raw_data": True, "supports_ai": False,
            "visualization_only": True, "analytical": False,
            "preview_kind": "cog", "supports_visual_enhancement": False,
        },
    },
    "airbus_oneatlas": {
        "key": "airbus_oneatlas",
        "name": "Airbus OneAtlas Data",
        "provider": "Airbus Defence and Space",
        "group": "Commercial EO",
        "gee_collection": "",
        "source_kind": "oneatlas_data_api",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": None,
        "revisit_days": None,
        "start_year": None,
        "cloud_property": "cloudCover",
        "cloud_mask_techniques": None,
        "license": "OneAtlas customer contract and product-specific terms",
        "configuration_message": "OneAtlas API key, account workspace ID, and a streaming entitlement are required. Preview is available only for returned scenes with WMTS rights.",
        "description": "OneAtlas Data Living Library through the official OpenSearch API. Scene search is separate from STAC; RGB preview uses the authenticated provider WMTS only when the account response grants that right. No order or tasking is performed.",
        "capabilities": {
            "searchable": True, "downloadable": False, "supports_preview": True,
            "supports_compare": True, "supports_mosaic": False,
            "supports_bands": False, "supports_raw_data": False, "supports_ai": False,
            "visualization_only": True, "analytical": False,
            "preview_kind": "provider_tiles", "supports_visual_enhancement": False,
        },
    },
    "blacksky_spectra": {
        "key": "blacksky_spectra",
        "name": "BlackSky Spectra",
        "provider": "BlackSky",
        "group": "Commercial EO",
        "gee_collection": "",
        "source_kind": "blacksky_spectra",
        "visualization": "rgb",
        "color_mode": "natural",
        "resolution_m": None,
        "revisit_days": None,
        "start_year": None,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "license": "BlackSky customer contract and product-specific terms",
        "configuration_message": "Belum dapat dicari: dokumentasi API, skema autentikasi, hak preview, dan kontrak akun pelanggan SAVEGEO belum tersedia.",
        "description": "Katalog belum aktif. SAVEGEO belum memiliki kontrak API pelanggan resmi untuk BlackSky Spectra; endpoint, skema, dan hak akses tidak diasumsikan.",
        "capabilities": {
            "searchable": False, "downloadable": False, "supports_preview": False,
            "supports_compare": False, "supports_mosaic": False,
            "supports_bands": False, "supports_raw_data": False, "supports_ai": False,
            "visualization_only": True, "analytical": False,
            "preview_kind": None, "supports_visual_enhancement": False,
        },
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "group": "USGS / Landsat",
        "gee_collection": "LANDSAT/LC08/C02/T1_L2",
        "visualization": "rgb",
        "supports_mosaic": True,
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
        "group": "USGS / Landsat",
        "gee_collection": "LANDSAT/LC09/C02/T1_L2",
        "visualization": "rgb",
        "supports_mosaic": True,
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B4", "green": "SR_B3", "blue": "SR_B2"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 2021,
        "description": "Optik 30m - penerus Landsat 8, sejak 2021.",
    },
    "landsat4": {
        "key": "landsat4",
        "name": "Landsat 4 TM - Surface Reflectance",
        "provider": "USGS / NASA",
        "group": "USGS / Landsat",
        "gee_collection": "LANDSAT/LT04/C02/T1_L2",
        "visualization": "rgb",
        "supports_mosaic": True,
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B3", "green": "SR_B2", "blue": "SR_B1"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 1982,
        "description": "USGS Landsat 4 TM Collection 2 Level 2: arsip surface reflectance 30m dari Agustus 1982 hingga Juni 1993.",
    },
    "landsat5": {
        "key": "landsat5",
        "name": "Landsat 5 TM - Surface Reflectance",
        "provider": "USGS / NASA",
        "group": "USGS / Landsat",
        "gee_collection": "LANDSAT/LT05/C02/T1_L2",
        "visualization": "rgb",
        "supports_mosaic": True,
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B3", "green": "SR_B2", "blue": "SR_B1"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 1984,
        "description": "USGS Landsat 5 TM Collection 2 Level 2: arsip surface reflectance 30m dari Maret 1984 hingga Mei 2012.",
    },
    "landsat7": {
        "key": "landsat7",
        "name": "Landsat 7 ETM+ - Surface Reflectance",
        "provider": "USGS / NASA",
        "group": "USGS / Landsat",
        "gee_collection": "LANDSAT/LE07/C02/T1_L2",
        "visualization": "rgb",
        "supports_mosaic": True,
        "color_mode": "natural",
        "band_role_map": {"red": "SR_B3", "green": "SR_B2", "blue": "SR_B1"},
        "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
        "vis_min": 0, "vis_max": 0.3,
        "cloud_property": "CLOUD_COVER",
        "cloud_mask_techniques": None,
        "resolution_m": 30, "revisit_days": 16, "start_year": 1999,
        "description": "USGS Landsat 7 ETM+ Collection 2 Level 2: arsip surface reflectance 30m dari Mei 1999 hingga Januari 2024. Setelah Mei 2003, beberapa scene memiliki celah SLC-off.",
    },
    "sentinel1": {
        "key": "sentinel1",
        "name": "Sentinel-1 (SAR)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-1",
        "gee_collection": "COPERNICUS/S1_GRD",
        "visualization": "sar",
        "supports_mosaic": True,
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
    "alos_avnir2": {
        "key": "alos_avnir2",
        "name": "ALOS / AVNIR-2 ORI",
        "provider": "JAXA",
        "group": "ALOS / JAXA",
        "gee_collection": "JAXA/ALOS/AVNIR-2/ORI",
        "visualization": "rgb",
        "supports_mosaic": True,
        "color_mode": "natural",
        "band_role_map": {"red": "B3", "green": "B2", "blue": "B1"},
        "vis_min": 0, "vis_max": 255,
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 10, "revisit_days": 46, "start_year": 2006,
        "license": "JAXA ALOS-ORI attribution required",
        "description": "Citra optik orthorectified ALOS DAICHI dari sensor AVNIR-2, 10m, tersedia sekitar 2006-2011 dengan cakupan utama Jepang. Sumber asli JAXA wajib dicantumkan.",
    },
    "alos_palsar_yearly": {
        "key": "alos_palsar_yearly",
        "name": "ALOS PALSAR/PALSAR-2 - Mosaik SAR tahunan",
        "provider": "JAXA EORC",
        "group": "ALOS / JAXA",
        "gee_collection": "JAXA/ALOS/PALSAR/YEARLY/SAR_EPOCH",
        "visualization": "sar",
        "supports_mosaic": True,
        "sar_value_mode": "dn",
        "sar_bands": {"vv": "HH", "vh": "HV"},
        "sar_grayscale_vis": {"band": "HH", "min": -30, "max": 0},
        "sar_composite_vis": {"min": [-30, -30, -5], "max": [0, 0, 15]},
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 25, "revisit_days": 365, "start_year": 2015,
        "license": "JAXA EORC attribution required",
        "description": "Mosaik global SAR tahunan 25m dari ALOS PALSAR/PALSAR-2. Nilai HH/HV dikonversi dari DN ke gamma-naught dB; bukan akuisisi single-scene dengan jam presisi. Tidak ada data 2011-2014.",
    },
    "alos2_palsar2_scansar": {
        "key": "alos2_palsar2_scansar",
        "name": "ALOS-2 PALSAR-2 ScanSAR (L2.2)",
        "provider": "JAXA EORC",
        "group": "ALOS / JAXA",
        "gee_collection": "JAXA/ALOS/PALSAR-2/Level2_2/ScanSAR",
        "visualization": "sar",
        "supports_mosaic": True,
        "sar_value_mode": "dn",
        "sar_bands": {"vv": "HH", "vh": "HV"},
        "required_polarizations": ["HH", "HV"],
        "sar_grayscale_vis": {"band": "HH", "min": -30, "max": 0},
        "sar_composite_vis": {"min": [-30, -30, -5], "max": [0, 0, 15]},
        "cloud_property": None,
        "cloud_mask_techniques": None,
        "resolution_m": 25, "revisit_days": 14, "start_year": 2014,
        "license": "JAXA EORC attribution required",
        "description": "Scene SAR ALOS-2 PALSAR-2 ScanSAR Level 2.2, 25m, orthorectified dan terrain-corrected. Pencarian dibatasi ke scene dual-polarisasi HH+HV agar mode komposit aman digunakan.",
    },
    "sentinel3": {
        "key": "sentinel3",
        "name": "Sentinel-3 (OLCI)",
        "provider": "ESA / Copernicus",
        "group": "Sentinel-3",
        "gee_collection": "COPERNICUS/S3/OLCI",
        "visualization": "rgb",
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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
        "supports_mosaic": True,
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


def provider_capabilities(meta: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider capabilities for the imagery catalog response."""
    source_kind = str(meta.get("source_kind") or "gee")
    visualization = str(meta.get("visualization") or "rgb")
    declared = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    visualization_only = source_kind in {"esri_wayback", "google_basemap", "apple_basemap", "himawari"}
    commercial = source_kind in {
        "planet", "maxar", "commercial", "planet_stac", "vantor_stac", "iceye_stac",
        "oneatlas_data_api", "blacksky_spectra",
    }
    open_data = bool(meta.get("open_data", not commercial))
    defaults: dict[str, Any] = {
        "searchable": source_kind != "himawari",
        "downloadable": bool(meta.get("gee_collection") or "stac" in source_kind),
        "analytical": not visualization_only,
        "visualization_only": visualization_only,
        "supports_time": True,
        "supports_cloud_filter": bool(meta.get("cloud_property")),
        # A single-band product still exposes a physical band (for example
        # NO2 or night-light radiance); it just does not expose an RGB trio.
        "supports_bands": visualization in {"rgb", "sar", "single_band"},
        "supports_raw_data": bool(meta.get("gee_collection") or "stac" in source_kind),
        "supports_ai": bool(meta.get("supports_ai", not visualization_only and open_data)),
        "requires_authentication": commercial,
        "commercial": commercial,
        "open_data": open_data,
        "supports_preview": True,
        "supports_compare": True,
        "supports_mosaic": bool(meta.get("supports_mosaic")),
        "supports_visual_enhancement": not visualization_only and not commercial,
        "preview_kind": (
            "provider_tiles" if source_kind == "oam_stac"
            else "cog" if "stac" in source_kind
            else "direct_xyz" if source_kind == "big_ctsrt"
            else "gee" if meta.get("gee_collection")
            else None
        ),
        "supports_scene_metadata": "stac" in source_kind or source_kind in {"big_ctsrt", "oneatlas_data_api"},
    }
    defaults.update(declared)
    # Keep the legacy top-level mosaic flag authoritative while returning it
    # through the capability object used by new UI code.
    defaults["supports_mosaic"] = bool(meta.get("supports_mosaic", declared.get("supports_mosaic", False)))
    defaults["supports_cloud_filter"] = bool(meta.get("cloud_property")) and bool(
        declared.get("supports_cloud_filter", True)
    )
    return defaults


def resolve_imagery_provider(key: str | None) -> str:
    return key if key in IMAGERY_PROVIDERS else DEFAULT_IMAGERY_PROVIDER


def get_imagery_provider_meta(key: str | None) -> dict[str, Any]:
    return IMAGERY_PROVIDERS[resolve_imagery_provider(key)]
