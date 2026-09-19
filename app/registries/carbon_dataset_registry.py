"""
carbon_dataset_registry.py — Central registry for carbon reference datasets.

Single source of truth for GEE asset IDs, band names, transforms, units,
and target pool metadata. All backend touch points (app.py, training/) read
from here instead of maintaining their own dataset dicts.

See: backend/docs/carbon-datasets.md
     backend/docs/model-registry-strategy.md
"""
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Registry metadata (no GEE dependency)
# ─────────────────────────────────────────────

# fmt: off
CARBON_DATASET_REGISTRY: dict[str, dict] = {
    "WCMC": {
        "key":         "WCMC",
        "name":        "WCMC Carbon Density",
        "full_name":   "UN WCMC Global Biomass Carbon Density v1.0",
        "gee_id":      "WCMC/biomass_carbon_density/v1_0",
        "gee_type":    "ImageCollection",
        "band":        "carbon_tonnes_per_ha",   # fallback: "carbon"
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  300,
        "year":        2010,
        "year_range":  [2010, 2010],
        "transform":   "none",
        "description": "Global carbon density map circa 2010. Training target for WCMC-compatible models.",
        "notes":       "Band name varies between 'carbon_tonnes_per_ha' and 'carbon'. Already in C units — no 0.47 needed.",
    },
    "ESA_CCI": {
        "key":         "ESA_CCI",
        "name":        "ESA CCI Biomass",
        "full_name":   "ESA CCI Aboveground Biomass V4 AGB",
        "gee_id":      "ESA/CCI/Biomass/V4/AGB",
        "gee_type":    "ImageCollection",
        "band":        "agb",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  100,
        "year":        2020,
        "year_range":  [2010, 2020],
        "transform":   "multiply_0.47",
        "description": "ESA CCI AGB map. Band 'agb' is Mg biomass/ha; multiplied by 0.47 to yield Mg C/ha.",
        "notes":       "Available years: 2010, 2017, 2018, 2019, 2020. Nearest year selected if exact not found.",
    },
    "GEDI": {
        "key":         "GEDI",
        "name":        "GEDI L4B Biomass",
        "full_name":   "NASA GEDI Level 4B Gridded Biomass Density",
        "gee_id":      "LARSE/GEDI/GEDI04_B_002",
        "gee_type":    "Image",
        "band":        "MU",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  1000,
        "year":        2020,
        "year_range":  [2019, 2023],
        "transform":   "multiply_0.47",
        "description": "NASA GEDI L4B gridded AGBD at 1 km. MU band (mean unmasked biomass) × 0.47.",
        "notes":       "MU is biomass density Mg/ha. Multiply by 0.47 for carbon. Unmasked to 0 for training sampling.",
    },
    "GEDI_L4A_MONTHLY": {
        "key":         "GEDI_L4A_MONTHLY",
        "name":        "GEDI L4A Monthly",
        "full_name":   "NASA GEDI Level 4A Monthly AGBD Composite",
        "gee_id":      "LARSE/GEDI/GEDI04_A_002_MONTHLY",
        "gee_type":    "ImageCollection",
        "band":        "agbd",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  25,
        "year":        2020,
        "year_range":  [2019, 2023],
        "transform":   "multiply_0.47",
        "description": "GEDI L4A monthly AGBD composite at 25 m. agbd × 0.47 for carbon density.",
        "notes":       "Filter by year to build annual mosaic via median. Higher resolution than L4B.",
    },
    "GEDI_L4B_STACK": {
        "key":         "GEDI_L4B_STACK",
        "name":        "GEDI L4B + Predictor Stack",
        "full_name":   "NASA GEDI L4B with S2/S1/DEM/DW Predictor Stack",
        "gee_id":      "LARSE/GEDI/GEDI04_B_002",
        "gee_type":    "Image",
        "band":        "MU",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  1000,
        "year":        2020,
        "year_range":  [2019, 2023],
        "transform":   "multiply_0.47",
        "description": "GEDI L4B with full predictor stack (S2, S1, DEM, Dynamic World). GEE model comparison workflow.",
        "notes":       "Same GEE source as GEDI key. Separate key for workflows that use the full predictor stack.",
    },
    "GEDI_L4D": {
        "key":         "GEDI_L4D",
        "name":        "GEDI L4D Imputed AGBD",
        "full_name":   "NASA GEDI Level 4D Imputed Waveforms AGBD",
        "gee_id":      "LARSE/GEDI/GEDI04_D_002",
        "gee_type":    "ImageCollection",
        "band":        "agbd",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  30,
        "year":        2023,
        "year_range":  [2019, 2023],
        "transform":   "multiply_0.47",
        "description": "GEDI L4D 30 m imputed AGBD from k-NN fusion of GEDI footprint products and Landsat time series. agbd × 0.47 for carbon density.",
        "notes":       "Uses QA == 1 valid pixels where available. Product is centered on target year 2023; date range reflects source GEDI observations in the GEE catalog.",
    },
    "SPAWN": {
        "key":         "SPAWN",
        "name":        "Spawn & Gibbs AGB Carbon",
        "full_name":   "NASA/ORNL Global Aboveground Biomass Carbon Density (Spawn & Gibbs 2020)",
        "gee_id":      "NASA/ORNL/biomass_carbon_density/v1",
        "gee_type":    "ImageCollection",
        "band":        "agb",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  300,
        "year":        2010,
        "year_range":  [2010, 2010],
        "transform":   "none",
        "description": "Spawn & Gibbs (2020) global AGB carbon density. Already in Mg C/ha — no 0.47 needed.",
        "notes":       "Carbon units (not biomass). Uses same GEE collection as ORNL_AGB_BGB but only agb band.",
    },
    "ORNL_AGB_BGB": {
        "key":         "ORNL_AGB_BGB",
        "name":        "ORNL AGB+BGB Carbon",
        "full_name":   "NASA/ORNL Combined Aboveground + Belowground Biomass Carbon (Spawn 2020)",
        "gee_id":      "NASA/ORNL/biomass_carbon_density/v1",
        "gee_type":    "ImageCollection",
        "band":        "agb+bgb",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "combined_biomass_carbon",
        "resolution":  300,
        "year":        2010,
        "year_range":  [2010, 2010],
        "transform":   "add_agb_bgb",
        "description": "Combined aboveground + belowground carbon. Sum of agb and bgb bands, both already Mg C/ha.",
        "notes":       "Output band named 'agb' for pipeline compatibility but represents combined pool. Do not compare directly with aboveground-only datasets.",
    },
    "OPENLANDMAP_SOC": {
        "key":         "OPENLANDMAP_SOC",
        "name":        "OpenLandMap SOC",
        "full_name":   "OpenLandMap Soil Organic Carbon Density (USDA-6A1C) v02",
        "gee_id":      "OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02",
        "gee_type":    "Image",
        "band":        "b0",
        "output_band": "agb",
        "unit":        "g/kg",
        "target_pool": "soil_organic_carbon",
        "resolution":  250,
        "year":        2019,
        "year_range":  [2019, 2019],
        "transform":   "none",
        "description": "Soil organic carbon density at 0 cm depth in g/kg. SOC pool — do NOT mix totals with aboveground carbon.",
        "notes":       "Unit is g/kg (not Mg C/ha). Depth-to-mass conversion required for stock totals. Different pool from aboveground biomass.",
    },
    "ESA_CCI_SATIO_AGB": {
        "key":         "ESA_CCI_SATIO_AGB",
        "name":        "ESA CCI AGB (sat-io)",
        "full_name":   "ESA CCI Aboveground Biomass via sat-io Community Asset",
        "gee_id":      "projects/sat-io/open-datasets/ESA/ESA_CCI_AGB",
        "gee_type":    "ImageCollection",
        "band":        "AGB",
        "output_band": "agb",
        "unit":        "Mg C/ha",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  100,
        "year":        2020,
        "year_range":  [2010, 2020],
        "transform":   "multiply_0.47",
        "description": "ESA CCI AGB via sat-io community GEE asset. AGB band in Mg biomass/ha × 0.47.",
        "notes":       "Community asset — verify band name with bandNames() before production use. May not be accessible in all GEE projects.",
        "deprecated":  True,
        "replacement_key": "ESA_CCI_BIOMASS_V7_COG",
    },
    # Legacy proxy — kept for backward compatibility. Use GEDI or ESA_CCI instead.
    "Simard": {
        "key":         "Simard",
        "name":        "Simard Forest Height (proxy)",
        "full_name":   "Simard Global Forest Height → Biomass proxy (Hansen treecover × 2.0)",
        "gee_id":      "UMD/hansen/global_forest_change_2023_v1_11",
        "gee_type":    "Image",
        "band":        "treecover2000",
        "output_band": "agb",
        "unit":        "Mg/ha (proxy)",
        "target_pool": "aboveground_biomass_carbon",
        "resolution":  1000,
        "year":        2011,
        "year_range":  [2011, 2011],
        "transform":   "multiply_2.0",
        "description": "Legacy Hansen tree cover × 2.0 proxy. Not a real biomass dataset. Retained for backward compatibility.",
        "notes":       "DEPRECATED. Use GEDI, ESA_CCI, or SPAWN for actual biomass reference.",
        "deprecated":  True,
        "replacement_key": "CTREES_AGB_100M",
    },
}
# fmt: on

# ─────────────────────────────────────────────
# ArcGIS Living Atlas carbon datasets
# provider_type = "arcgis_living_atlas" → stats via computeHistograms,
# tiles via /api/arcgis/tiles/<key>/{z}/{y}/{x}
# ─────────────────────────────────────────────
# fmt: off
CARBON_ARCGIS_REGISTRY: dict[str, dict] = {
    "WCMC_Carbon_ArcGIS": {
        "key":              "WCMC_Carbon_ArcGIS",
        "provider_type":    "arcgis_living_atlas",
        "name":             "WCMC Biomass Carbon Density (ArcGIS)",
        "full_name":        "WCMC Global Above & Belowground Biomass Carbon Density — ArcGIS Living Atlas",
        "arcgis_service_url": "https://geoxc-prod-im.bd.esri.com/arcgis/rest/services/biomass_carbon_density/ImageServer",
        "arcgis_item_id":   None,
        "band":             "Band_1",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_belowground_biomass_carbon",
        "resolution":       300,
        "year":             2010,
        "year_range":       [2010, 2010],
        "time_aware":       False,
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
        "transform":        "none",
        "description":      "Global AGB+BGB biomass carbon density, 300m resolution, circa 2010. Source: UN WCMC, hosted on ArcGIS Living Atlas.",
        "attribution":      "UN WCMC / ArcGIS Living Atlas",
        "limitations":      ["Static 2010 snapshot; does not reflect recent forest loss or gain.", "AGB+BGB combined — not directly comparable to AGB-only models."],
    },
    "UNEP_WCMC_Biomass": {
        "key":              "UNEP_WCMC_Biomass",
        "provider_type":    "arcgis_living_atlas",
        "name":             "UNEP-WCMC World Biomass Carbon",
        "full_name":        "UNEP-WCMC Above & Belowground Biomass Carbon (tonnes C/ha)",
        "arcgis_service_url": "https://data-gis.unep-wcmc.org/portal/sharing/servers/8a8d4e24683a46e6b039aea78c8af20f/rest/services/World_Biomass_Carbon/ImageServer",
        "arcgis_item_id":   None,
        "band":             "Band_1",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_belowground_biomass_carbon",
        "resolution":       300,
        "year":             2010,
        "year_range":       [2010, 2010],
        "time_aware":       False,
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["fff7fb","ece2f0","d0d1e6","a6bddb","67a9cf","3690c0","02818a","016c59","014636"],
        "transform":        "none",
        "description":      "Global AGB+BGB biomass carbon density from UNEP-WCMC World Conservation Monitoring Centre. 300m, circa 2010.",
        "attribution":      "UNEP-WCMC / ArcGIS Living Atlas",
        "histogram_geom_type": "esriGeometryEnvelope",  # this service requires bbox, not polygon
        "limitations":      ["Static 2010 snapshot.", "Use bbox envelope for statistics (polygon geometry unsupported by this service)."],
    },
    "UNEP_WCMC_Biomass_SOC": {
        "key":              "UNEP_WCMC_Biomass_SOC",
        "provider_type":    "arcgis_living_atlas",
        "name":             "UNEP-WCMC Biomass + Soil Carbon",
        "full_name":        "UNEP-WCMC Above/Belowground Biomass + Soil Organic Carbon to 1m (tonnes C/ha)",
        "arcgis_service_url": "https://data-gis.unep-wcmc.org/portal/sharing/servers/374a99fc76574f72bb8c71af7b428d0a/rest/services/World_Biomass_and_Soil_Carbon/ImageServer",
        "arcgis_item_id":   None,
        "band":             "Band_1",
        "unit":             "Mg C/ha",
        "target_pool":      "total_ecosystem_carbon",
        "resolution":       300,
        "year":             2010,
        "year_range":       [2010, 2010],
        "time_aware":       False,
        "vis_min":          0,
        "vis_max":          600,
        "vis_palette":      ["ffffe5","f7fcb9","d9f0a3","addd8e","78c679","41ab5d","238443","006837","004529"],
        "transform":        "none",
        "description":      "Combined AGB+BGB biomass carbon + soil organic carbon to 1m depth. High values in peatland regions (incl. Sumatra/Kalimantan). 300m, circa 2010.",
        "attribution":      "UNEP-WCMC / ArcGIS Living Atlas",
        "histogram_geom_type": "esriGeometryEnvelope",
        "limitations":      ["Includes SOC to 1m — much higher than AGB-only datasets; not compatible with AGB models.", "Use bbox envelope for statistics."],
    },
}
# fmt: on

# ─────────────────────────────────────────────
# External / Open-Data raster carbon datasets
# provider_type = "external_raster" → stats via HTTP REST/WCS/COG;
# COG entries use AOI-windowed reads and expose no GEE tile URL.
# ─────────────────────────────────────────────
# fmt: off
CARBON_EXTERNAL_REGISTRY: dict[str, dict] = {
    "SOILGRIDS_SOC_30CM": {
        "key":              "SOILGRIDS_SOC_30CM",
        "provider_type":    "external_raster",
        "name":             "SoilGrids SOC 0–30cm",
        "full_name":        "ISRIC SoilGrids v2.0 — Soil Organic Carbon 0–30 cm depth-weighted mean",
        "service_url":      "https://rest.isric.org/soilgrids/v2.0/properties/query",
        "source_url":       "https://www.isric.org/explore/soilgrids",
        "band":             "soc",
        "unit":             "g/kg",
        "target_pool":      "soil_organic_carbon",
        "resolution":       250,
        "year":             2017,
        "year_range":       [2017, 2017],
        "time_aware":       False,
        "transform":        "divide_10",  # native dg/kg → g/kg
        "description":      "ISRIC SoilGrids v2.0 SOC at 0–30 cm depth-weighted mean, 250 m global. Point queries via REST API. SOC pool — do not compare totals with aboveground biomass datasets.",
        "attribution":      "ISRIC — World Soil Information (SoilGrids v2.0 / Poggio et al. 2021)",
        "limitations":      [
            "SOC pool — not comparable with aboveground biomass carbon.",
            "No tile rendering: only sampled point statistics available in current implementation.",
            "API rate-limited; large AOI statistics use ≤100 sample points.",
        ],
        "vis_min":          0,
        "vis_max":          80,
        "vis_palette":      ["fff5eb","fee6ce","fdd0a2","fdae6b","fd8d3c","f16913","d94801","8c2d04"],
        "ingestion_method": "soilgrids_rest",
        "training_capable": True,
        "depths":           ["0-5cm", "5-15cm", "15-30cm"],
    },
    "GLOBAL_MANGROVE_WATCH_AGB": {
        "key":              "GLOBAL_MANGROVE_WATCH_AGB",
        "provider_type":    "external_raster",
        "name":             "GMW Mangrove Extent (mask only)",
        "full_name":        "JAXA Global Mangrove Watch — extent/change mask (1996-2020, 25 m)",
        "service_url":      None,
        "source_url":       "https://www.eorc.jaxa.jp/ALOS/en/dataset/gmw_e.htm",
        "band":             "extent",
        "unit":             "binary mask",
        "target_pool":      "mangrove_extent_mask",
        "resolution":       25,
        "year":             2020,
        "year_range":       [1996, 2020],
        "time_aware":       False,
        "transform":        "none",
        "description":      "JAXA Global Mangrove Watch is an extent/change mask, not an above-ground biomass raster. Keep it for mangrove masking only; use CTREES_AGB_100M or a verified mangrove AGB product for carbon density.",
        "attribution":      "JAXA / Global Mangrove Watch Consortium",
        "limitations":      [
            "Mangrove ecosystems only — results invalid for terrestrial forest areas.",
            "This registry entry is not a biomass/carbon reference and cannot produce Mg C/ha statistics.",
            "No direct AGB download is configured; do not select it for carbon estimation.",
        ],
        "vis_min":          0,
        "vis_max":          1,
        "vis_palette":      ["f7fcfd","e0ecf4","bfd3e6","9ebcda","8c96c6","88419d","6e016b"],
        "training_capable": False,
        "deprecated":       True,
        "replacement_key":  "CTREES_AGB_100M",
        "ingestion_method": "mask_only",
    },
    # ── Experimental / 2026 ─────────────────────────────────────────────────
    "ESA_BIOMASS_2026": {
        "key":              "ESA_BIOMASS_2026",
        "provider_type":    "esa_biomass_external",
        "name":             "ESA Biomass 2026 (Experimental)",
        "full_name":        "ESA Biomass Mission — Forest Woody Biomass Carbon (2026, experimental)",
        "service_url":      "https://earth.esa.int/eogateway/missions/biomass",
        "source_url":       "https://earth.esa.int/eogateway/missions/biomass",
        # raster_url: admin-configured via env var ESA_BIOMASS_2026_URL or set here as a public COG URI.
        # Leave None until a product URL is confirmed.
        "raster_url":       None,
        "band":             "agb",
        "output_band":      "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "forest_woody_biomass_carbon",
        "resolution":       25,
        "year":             2026,
        "year_range":       [2026, 2026],
        "time_aware":       False,
        # biomass → carbon: multiply by 0.47 only if the product delivers Mg biomass/ha
        "transform":        "multiply_0.47",
        "ingestion_method": "cloud_geotiff_ee",   # loaded via ee.Image.loadGeoTIFF(url)
        "experimental":     True,
        "training_capable": False,  # enable once raster_url is verified and stable
        "description":      (
            "ESA BIOMASS mission data, open data since 2026-01-26. "
            "Experimental provider: availability depends on configured ESA product raster. "
            "URL configured via env var ESA_BIOMASS_2026_URL or registry raster_url field. "
            "Forest-focused, global mission; coverage and accuracy depend on ESA product available."
        ),
        "attribution":      "European Space Agency (ESA) BIOMASS mission",
        "limitations":      [
            "Experimental: raster URL must be configured before use (env var ESA_BIOMASS_2026_URL or registry raster_url).",
            "If URL not set, backend returns clear error — not a silent crash.",
            "Forest woody biomass carbon pool only; not valid for non-forest areas.",
            "Unit interpretation (biomass vs. carbon) must match ESA product documentation. multiply_0.47 applied if product is Mg biomass/ha.",
            "Not available for training until raster_url is verified and training_capable is set to True.",
            "Coverage and accuracy depend on ESA BIOMASS mission product maturity at time of access.",
        ],
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
        "launch_date":      "2024-04",
        "data_open_date":   "2026-01-26",
    },
    "CHLORIS_AGB_STOCK": {
        "key":              "CHLORIS_AGB_STOCK",
        "provider_type":    "chloris_platform",
        "name":             "Chloris AGB Carbon Stock",
        "full_name":        "Chloris Platform Annual Above-Ground Biomass Stock",
        "service_url":      "https://app.chloris.earth/api/reportingUnit/",
        "source_url":       "https://app.chloris.earth/",
        # Optional override: set CHLORIS_AGB_STOCK_URL or CHLORIS_STOCK_URL to
        # a direct HTTPS/gs:// GeoTIFF. Otherwise the adapter first resolves a
        # licensed Chloris downloads.json and then falls back to the public
        # Planetary Computer chloris-biomass STAC collection.
        "raster_url":       None,
        "band":             "stock",
        "output_band":      "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_biomass_carbon",
        "resolution":       4633,
        "year":             2019,
        "year_range":       [2003, 2019],
        "time_aware":       True,
        "transform":        "multiply_0.47",
        # The open STAC stock asset stores tonnes of biomass per pixel. It is
        # converted to Mg C/ha after loading; licensed Chloris rasters retain
        # the x0.47 biomass-to-carbon transform above.
        "planetary_computer_transform": "tonnes_per_pixel_to_mg_c_ha",
        "ingestion_method": "chloris_downloads_index",
        "chloris_product":  "stock",
        "requires_auth":    False,
        "experimental":     False,
        "training_capable": False,
        "description":      (
            "Chloris annual above-ground biomass stock. The backend prefers a "
            "licensed Chloris reporting-unit GeoTIFF when configured and otherwise "
            "loads the public Planetary Computer chloris-biomass STAC asset "
            "(2003-2019, ~4.6 km). Open stock values are converted from tonnes per "
            "pixel to Mg C/ha using a 0.47 carbon fraction."
        ),
        "attribution":      "Chloris Geospatial / Microsoft Planetary Computer",
        "limitations":      [
            "The public Planetary Computer fallback covers 2003-2019 at circa 4.6 km; licensed Chloris reporting units may provide other years/resolutions.",
            "The public collection is licensed CC BY-NC-SA-4.0 and is for non-commercial/share-alike use; review the license before production use.",
            "Earth Engine loadGeoTIFF must be able to fetch the signed COG URL; direct URLs must be accessible from Earth Engine.",
            "Uses above-ground biomass stock only; belowground and soil carbon are not included.",
            "Open stock values are per pixel, then normalized to Mg C/ha using pixel area and a 0.47 carbon fraction.",
        ],
        "vis_min":          0,
        "vis_max":          250,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
    },
    "HANSEN_TREECOVER_AGB_PROXY": {
        "key":              "HANSEN_TREECOVER_AGB_PROXY",
        "provider_type":    "external_raster",
        "name":             "Hansen Treecover AGB Proxy",
        "full_name":        "Hansen GFC 2023 — Treecover AGB Carbon Proxy (30 m)",
        "service_url":      None,  # tiled — no single URL
        "tile_url_template": "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2023-v1.11/Hansen_GFC-2023-v1.11_treecover2000_{lat_tile}_{lon_tile}.tif",
        "lossyear_url_template": "https://storage.googleapis.com/earthenginepartners-hansen/GFC-2023-v1.11/Hansen_GFC-2023-v1.11_lossyear_{lat_tile}_{lon_tile}.tif",
        "mask_lossyear":    True,
        "is_tiled":         True,
        "tile_size_deg":    10,
        "source_url":       "https://earthenginepartners.appspot.com/science-2013-global-forest",
        "band":             "treecover2000",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_biomass_carbon",
        "resolution":       30,
        "year":             2023,
        "year_range":       [2000, 2023],
        "time_aware":       False,
        "transform":        "multiply_0.94",  # % cover × 2.0 Mg biomass/ha × 0.47 C fraction ≈ × 0.94
        "description":      "Hansen GFC v1.11 treecover2000 band as AGB carbon proxy. Forest cover (%) × 0.94 ≈ Mg C/ha. S2 spectral features correlate strongly with tree cover — expected R² 0.4–0.6. PROXY: not a calibrated biomass map.",
        "notes":            "Retained as a first-class reference for existing Hansen-compatible models. Use CTREES_AGB_100M for new calibrated biomass workflows; do not mix model targets across datasets.",
        "attribution":      "Hansen/UMD/Google/USGS/NASA — Global Forest Change v1.11 (2023)",
        "limitations":      [
            "AGB PROXY only — treecover × 2.0 Mg/ha × 0.47 C fraction; not a validated biomass measurement.",
            "treecover2000 baseline — does not account for post-2000 deforestation without lossyear masking.",
            "No tile rendering; sampled statistics only.",
            "nodata=255 (non-land pixels); 0 = non-forest land, valid label.",
        ],
        "vis_min":          0,
        "vis_max":          100,
        "vis_palette":      ["fff5f0","fee0d2","fcbba1","fc9272","fb6a4a","ef3b2c","cb181d","a50f15","67000d"],
        "nodata":           255,
        "ingestion_method": "cog_rasterio",
        "training_capable": True,
        # GEE native dataset for tile visualization (stats/labels still from cog_rasterio)
        "gee_vis_id":   "UMD/hansen/global_forest_change_2023_v1_11",
        "gee_vis_band": "treecover2000",
    },
    "ESA_CCI_BIOMASS_COG": {
        "key":              "ESA_CCI_BIOMASS_COG",
        "provider_type":    "external_raster",
        "name":             "ESA CCI Biomass (COG, non-GEE)",
        "full_name":        "ESA CCI Aboveground Biomass V4.0 — public COG (CEDA archive, no GEE)",
        "service_url":      None,  # tiled — no single URL
        "tile_url_template": "https://dap.ceda.ac.uk/neodc/esacci/biomass/data/agb/maps/v4.0/geotiff/{year}/{tile_id}_ESACCI-BIOMASS-L4-AGB-MERGED-100m-{year}-fv4.0.tif?download=1",
        "is_tiled":         True,
        "tile_size_deg":    10,
        "source_url":       "https://catalogue.ceda.ac.uk/uuid/af60720c1e404a9e9d2c145d2b2ead4e",
        "band":             "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_biomass_carbon",
        "resolution":       100,
        "year":             2020,
        "year_range":       [2010, 2020],
        "available_years":  [2010, 2017, 2018, 2019, 2020],
        "time_aware":       False,
        "transform":        "multiply_0.47",  # ESA CCI band is Mg biomass/ha -> Mg C/ha
        "description":      (
            "ESA CCI Biomass v4.0 AGB map (Mg biomass/ha, x0.47 for carbon), 100 m, "
            "10x10 degree GeoTIFF tiles served anonymously (no login) from CEDA's dap.ceda.ac.uk "
            "archive. Verified 2026-07-09: HTTP 200, byte-range (accept-ranges) capable, no auth "
            "required — confirmed compatible with rasterio's /vsicurl/ windowed reads. "
            "Real calibrated AGB (not a proxy), unlike HANSEN_TREECOVER_AGB_PROXY."
        ),
        "attribution":      "ESA Climate Change Initiative (CCI) Biomass project / CEDA",
        "limitations": [
            ("Tile naming is SW-corner-based (e.g. N00E000, S10E110) — ocean/no-data tiles simply "
             "don't exist server-side (404), handled as a missing tile, not an error."),
            "Available years: 2010, 2017, 2018, 2019, 2020 only (no continuous annual coverage).",
            "No tile rendering; sampled/gridded statistics only via this non-GEE pipeline.",
        ],
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
        "nodata":           0,
        "ingestion_method": "cog_rasterio",
        "training_capable": True,
        "deprecated":       True,
        "replacement_key":  "ESA_CCI_BIOMASS_V7_COG",
    },
    "ESA_CCI_BIOMASS_V7_COG": {
        "key":              "ESA_CCI_BIOMASS_V7_COG",
        "provider_type":    "external_raster",
        "name":             "ESA CCI Biomass v7 (COG)",
        "full_name":        "ESA CCI Aboveground Biomass v7.0 — public COG (CEDA)",
        "service_url":      None,
        "tile_url_template": "https://dap.ceda.ac.uk/neodc/esacci/biomass/data/agb/maps/v7.0/geotiff/{year}/{tile_id}_ESACCI-BIOMASS-L4-AGB-MERGED-100m-{year}-fv7.0.tif?download=1",
        "is_tiled":         True,
        "tile_size_deg":    10,
        "source_url":       "https://catalogue.ceda.ac.uk/uuid/6429d1aafe1e43b9b414e4a5a7f8b903/",
        "band":             "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_biomass_carbon",
        "resolution":       100,
        "year":             2024,
        "year_range":       [2005, 2024],
        "available_years":  [2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024],
        "time_aware":       True,
        "transform":        "multiply_0.47",
        "description":      (
            "ESA CCI Biomass v7.0 annual forest above-ground biomass maps. "
            "Global 100 m GeoTIFF tiles are read directly from the public CEDA archive "
            "and converted from Mg biomass/ha to Mg C/ha."
        ),
        "attribution":      "ESA Climate Change Initiative Biomass / CEDA",
        "limitations":      [
            "Maps are available for 2005-2012 and 2015-2024; 2013-2014 are not available.",
            "Forest woody above-ground biomass only; do not interpret as belowground or soil carbon.",
            "CEDA archive access is public but remains subject to the ESA CCI Biomass terms and citation requirements.",
            "No GEE tile rendering; AOI statistics use windowed COG reads.",
        ],
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
        "nodata":           0,
        "ingestion_method": "cog_rasterio",
        "training_capable": True,
    },
    "CTREES_AGB_100M": {
        "key":              "CTREES_AGB_100M",
        "provider_type":    "external_raster",
        "name":             "CTrees Global AGB",
        "full_name":        "CTrees Global Aboveground Biomass Density (100 m)",
        "service_url":      None,
        "global_url_template": "https://ctrees-agb-100m-global.s3.us-west-2.amazonaws.com/cogs/global_agb_100m_landsat0024_all_{year}_densenet_l1_agb_mosaic_100m_base_cd_ts.tif",
        "uncertainty_url_template": "https://ctrees-agb-100m-global.s3.us-west-2.amazonaws.com/cogs/global_agb_100m_landsat0024_all_{year}_densenet_l1_agb_mosaic_100m_base_cd_ts_uncertainty_sem.tif",
        "source_url":       "https://registry.opendata.aws/ctrees-agb-100m-global/",
        "band":             "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "aboveground_biomass_carbon",
        "resolution":       100,
        "year":             2025,
        "year_range":       [2000, 2025],
        "available_years":  list(range(2000, 2026)),
        "time_aware":       True,
        # Raw CTrees COG values are int16 Mg biomass/ha scaled by 10.
        "transform":        "divide_10_multiply_0.47",
        "description":      (
            "CTrees global above-ground biomass density, annual 2000-2025 at 100 m. "
            "Public COGs are read with HTTP range requests over the requested AOI, "
            "then converted from scaled biomass to Mg C/ha."
        ),
        "attribution":      "CTrees / Yang, Saatchi et al.",
        "limitations":      [
            "Each global COG is tens of gigabytes; only AOI windows should be read.",
            "AGB above-ground stock only; uncertainty is available as a separate COG asset.",
            "Raw values use a scale factor of 10 and nodata=-9999 before conversion.",
        ],
        "vis_min":          0,
        "vis_max":          300,
        "vis_palette":      ["f7fcf5","e5f5e0","c7e9c0","a1d99b","74c476","41ab5d","238b45","006d2c","00441b"],
        "nodata":           -9999,
        "ingestion_method": "cog_rasterio",
        "training_capable": True,
    },
}
# fmt: on

# Keys exposed for training CLI choices. Deprecated keys remain here so
# existing model metadata can still be resolved; the UI labels them legacy.
TRAINING_DATASET_KEYS: list[str] = [
    "WCMC",
    "ESA_CCI",
    "GEDI",
    "GEDI_L4A_MONTHLY",
    "GEDI_L4B_STACK",
    "GEDI_L4D",
    "SPAWN",
    "ORNL_AGB_BGB",
    "OPENLANDMAP_SOC",
    "ESA_CCI_SATIO_AGB",
    # ArcGIS-sourced
    "WCMC_Carbon_ArcGIS",
    "UNEP_WCMC_Biomass",
    "UNEP_WCMC_Biomass_SOC",
    # External raster / open-data sourced (training_capable: True only)
    "SOILGRIDS_SOC_30CM",
    "HANSEN_TREECOVER_AGB_PROXY",
    "ESA_CCI_BIOMASS_COG",
    "ESA_CCI_BIOMASS_V7_COG",
    "CTREES_AGB_100M",
    "CHLORIS_AGB_STOCK",
]

# Datasets whose GEE loaders genuinely select an annual image/composite from
# the requested dataset_year. Static or multi-year products keep their registry
# baseline/coverage label even when the frontend sends dataset_year.
TEMPORAL_DATASET_KEYS = {
    "ESA_CCI",
    "GEDI_L4A_MONTHLY",
    "ESA_CCI_SATIO_AGB",
}


# ─────────────────────────────────────────────
# Metadata helpers (no GEE)
# ─────────────────────────────────────────────

def load_external_carbon_reference_ee(
    key: str,
    dataset_year: int | None = None,
    roi=None,
):
    """Load an external cloud GeoTIFF carbon reference via ee.Image.loadGeoTIFF().

    Supports datasets in CARBON_EXTERNAL_REGISTRY with ingestion_method='cloud_geotiff_ee'
    or 'chloris_downloads_index'.
    URL resolved from (in priority order):
        1. Env var  {KEY}_URL  (e.g. ESA_BIOMASS_2026_URL)
        2. Registry field  raster_url

    Returns ee.Image with band 'agb'. Transform (multiply_0.47 etc.) applied.

    Raises ValueError if key unknown, ingestion_method mismatch, or URL not configured.
    """
    import os

    import ee

    meta = CARBON_EXTERNAL_REGISTRY.get(key)
    if meta is None:
        raise ValueError(f"Unknown external carbon dataset: '{key}'. Not in CARBON_EXTERNAL_REGISTRY.")

    ingestion_method = meta.get("ingestion_method")
    if ingestion_method not in {"cloud_geotiff_ee", "chloris_downloads_index"}:
        raise ValueError(
            f"Dataset '{key}' does not use cloud_geotiff_ee loading "
            f"(ingestion_method={ingestion_method!r}). "
            "Use ExternalRasterProvider for REST/rasterio-based datasets."
        )

    raster_url = (
        os.getenv(f"{key}_URL")
        or os.getenv(f"{key.upper()}_URL")
        or meta.get("raster_url")
    )
    resolved_metadata: dict = {}
    if not raster_url and ingestion_method == "chloris_downloads_index":
        from app.services.chloris_service import resolve_chloris_download

        try:
            resolved = resolve_chloris_download(
                product=meta.get("chloris_product", "stock"),
                year=dataset_year,
            )
            raster_url = resolved.url
            resolved_metadata = resolved.metadata
        except Exception as exc:  # noqa: BLE001 - surface a user-actionable setup error
            raise ValueError(
                f"Could not resolve Chloris download for '{key}'. "
                "Set CHLORIS_AGB_STOCK_URL/CHLORIS_STOCK_URL directly, or configure "
                "CHLORIS_DATA_PATH, or configure CHLORIS_ORGANIZATION_ID plus "
                "CHLORIS_REFRESH_TOKEN/CHLORIS_ID_TOKEN. The public Planetary Computer "
                "fallback requires pystac-client and planetary-computer. Original error: "
                f"{exc}"
            ) from exc

    if not raster_url:
        raise ValueError(
            f"No raster URL configured for '{key}'. "
            f"Set environment variable '{key.upper()}_URL' to a public Cloud GeoTIFF URI "
            "(gs://... or HTTPS COG), or set 'raster_url' in CARBON_EXTERNAL_REGISTRY."
        )

    logger.info(f"Loading external carbon reference via GEE: {key} from {raster_url}")
    try:
        img = ee.Image.loadGeoTIFF(raster_url).select(0).rename("agb")
    except Exception as load_err:
        raise ValueError(
            f"ee.Image.loadGeoTIFF failed for '{key}' (URL: {raster_url}). "
            f"Verify the URI is a public Cloud-Optimized GeoTIFF. Original error: {load_err}"
        ) from load_err

    if resolved_metadata.get("source") == "planetary_computer_stac":
        # The STAC collection declares 2147483647 as the uint32 nodata value
        # for the annual stock asset. Mask it before converting units so a
        # missing pixel cannot become an extreme carbon-density outlier.
        img = img.updateMask(img.neq(2147483647))

    if roi is not None:
        img = img.clip(roi)

    transform = meta.get("transform", "none")
    if resolved_metadata.get("source") == "planetary_computer_stac":
        transform = meta.get("planetary_computer_transform", transform)
    if transform == "multiply_0.47":
        img = img.multiply(0.47).rename("agb")
    elif transform == "tonnes_per_pixel_to_mg_c_ha":
        # Planetary Computer's open Chloris stock asset is documented as
        # tonnes of biomass per pixel. Convert to Mg C/ha for the common
        # carbon-analysis contract used by this service.
        pixel_area_ha = ee.Image.pixelArea().divide(10000)
        img = img.multiply(0.47).divide(pixel_area_ha).rename("agb")
    elif transform == "multiply_2.0":
        img = img.multiply(2.0).rename("agb")
    elif transform.startswith("multiply_"):
        try:
            factor = float(transform.split("_", 1)[1])
            img = img.multiply(factor).rename("agb")
        except ValueError:
            pass

    return img


def is_arcgis_carbon_dataset(key: str) -> bool:
    """Return True if key is an ArcGIS Living Atlas carbon dataset."""
    return key in CARBON_ARCGIS_REGISTRY


def get_arcgis_carbon_meta(key: str) -> dict | None:
    """Return ArcGIS carbon dataset metadata, or None if not found."""
    return CARBON_ARCGIS_REGISTRY.get(key)


def is_external_carbon_dataset(key: str) -> bool:
    """Return True if key is an external raster / open-data carbon dataset."""
    return key in CARBON_EXTERNAL_REGISTRY


def get_external_carbon_meta(key: str) -> dict | None:
    """Return external raster carbon dataset metadata, or None if not found."""
    return CARBON_EXTERNAL_REGISTRY.get(key)


def get_dataset_meta(key: str, dataset_year: int | None = None) -> dict:
    """
    Return metadata dict for a dataset key.

    For time-series datasets the returned 'year' field reflects dataset_year
    when provided. Static and multi-year products keep their registry baseline
    or coverage label so the UI does not imply a year that was not selected in
    GEE.
    Falls back to a minimal 'unknown' dict for unrecognised keys so callers
    don't have to guard against None.
    """
    # Check external raster registry
    if key in CARBON_EXTERNAL_REGISTRY:
        meta = dict(CARBON_EXTERNAL_REGISTRY[key])
        meta["is_temporal"] = meta.get("time_aware", False)
        meta["requested_year"] = dataset_year
        meta["gee_id"] = None
        return meta

    # Check ArcGIS registry
    if key in CARBON_ARCGIS_REGISTRY:
        meta = dict(CARBON_ARCGIS_REGISTRY[key])
        meta["is_temporal"] = False
        meta["requested_year"] = dataset_year
        meta["gee_id"] = None
        return meta

    entry = CARBON_DATASET_REGISTRY.get(key)
    if entry is None:
        logger.warning(f"Unknown dataset key: '{key}'")
        return {
            "key":         key,
            "name":        "Unknown",
            "full_name":   "Unknown",
            "gee_id":      None,
            "band":        None,
            "unit":        "Mg/ha",
            "target_pool": "unknown",
            "resolution":  "N/A",
            "year":        dataset_year,
            "year_range":  None,
            "transform":   "none",
            "description": "N/A",
            "notes":       "",
        }

    meta = dict(entry)
    meta["is_temporal"] = key in TEMPORAL_DATASET_KEYS
    meta["requested_year"] = dataset_year

    if dataset_year is not None and key in TEMPORAL_DATASET_KEYS:
        meta["year"] = dataset_year
    elif key not in TEMPORAL_DATASET_KEYS:
        year_range = meta.get("year_range")
        if isinstance(year_range, list) and len(year_range) == 2 and year_range[0] != year_range[1]:
            meta["year"] = f"{year_range[0]}-{year_range[1]}"

    return meta


def list_dataset_keys(include_legacy: bool = False) -> list[str]:
    """Return all registered dataset keys. Excludes 'Simard' by default."""
    keys = list(CARBON_DATASET_REGISTRY.keys())
    if not include_legacy:
        keys = [k for k in keys if k != "Simard"]
    return keys


# ─────────────────────────────────────────────
# GEE loader
# ─────────────────────────────────────────────

def load_carbon_reference_ee(
    key: str,
    dataset_year: int = 2020,
    roi=None,
    unmask_for_training: bool = False,
):
    """
    Load a carbon reference dataset from GEE and return an ee.Image
    with a single band named 'agb'.

    Args:
        key: Dataset key from CARBON_DATASET_REGISTRY.
        dataset_year: Year to select for time-series datasets.
        roi: Optional ee.Geometry — used for filtering ImageCollections.
        unmask_for_training: If True, unmask no-data pixels to 0 (needed for
            GEE sampling so sparse datasets don't block training point collection).

    Returns:
        ee.Image with band 'agb' in units defined by the dataset's unit field.

    Raises:
        ValueError: Unknown key or loading failure.
    """
    import ee  # local import — requires ee.Initialize() already called

    entry = CARBON_DATASET_REGISTRY.get(key)
    if entry is None:
        available = list_dataset_keys(include_legacy=True)
        raise ValueError(
            f"Unknown dataset key: '{key}'. Available: {available}"
        )

    logger.info(f"Loading carbon reference dataset: {key} (year={dataset_year})")

    try:
        img = _LOADERS[key](entry, dataset_year, roi, unmask_for_training, ee)
    except KeyError:
        raise ValueError(f"No GEE loader implemented for dataset key: '{key}'")

    if unmask_for_training:
        img = img.unmask(0)

    return img


# ─────────────────────────────────────────────
# Per-dataset GEE loader implementations
# ─────────────────────────────────────────────

def _loader_wcmc(entry, dataset_year, roi, unmask, ee):
    collection = ee.ImageCollection(entry["gee_id"])
    img = collection.first()
    band_names = img.bandNames().getInfo()
    logger.info(f"WCMC available bands: {band_names}")
    if "carbon_tonnes_per_ha" in band_names:
        return img.select("carbon_tonnes_per_ha").rename("agb")
    if "carbon" in band_names:
        return img.select("carbon").rename("agb")
    logger.warning("WCMC: unexpected band names, selecting band 0")
    return img.select(0).rename("agb")


def _loader_esa_cci(entry, dataset_year, roi, unmask, ee):
    collection = ee.ImageCollection(entry["gee_id"])
    if roi:
        collection = collection.filterBounds(roi)
    # Try requested year first; fall back to latest available
    year_collection = collection.filterDate(
        f"{dataset_year}-01-01", f"{dataset_year}-12-31"
    )
    size = year_collection.size().getInfo()
    if size == 0:
        logger.warning(f"ESA_CCI: no image for {dataset_year}, using latest")
        img = collection.sort("system:time_start", False).first()
    else:
        img = year_collection.first()
    return img.select("agb").multiply(0.47).rename("agb")


def _loader_gedi(entry, dataset_year, roi, unmask, ee):
    img = ee.Image(entry["gee_id"])
    return img.select("MU").multiply(0.47).rename("agb")


def _loader_gedi_l4a_monthly(entry, dataset_year, roi, unmask, ee):
    collection = ee.ImageCollection(entry["gee_id"]).filterDate(
        f"{dataset_year}-01-01", f"{dataset_year}-12-31"
    )
    if roi:
        collection = collection.filterBounds(roi)
    size = collection.size().getInfo()
    if size == 0:
        logger.warning(f"GEDI_L4A_MONTHLY: no images for {dataset_year}")
        # Fallback: any available
        collection = ee.ImageCollection(entry["gee_id"])
        if roi:
            collection = collection.filterBounds(roi)
    img = collection.select("agbd").median()
    return img.multiply(0.47).rename("agb")


def _loader_gedi_l4b_stack(entry, dataset_year, roi, unmask, ee):
    # Same GEE source as GEDI; key distinction is the workflow context
    img = ee.Image(entry["gee_id"])
    return img.select("MU").multiply(0.47).rename("agb")


def _loader_gedi_l4d(entry, dataset_year, roi, unmask, ee):
    collection = ee.ImageCollection(entry["gee_id"])
    if roi:
        collection = collection.filterBounds(roi)

    def mask_valid(img):
        band_names = img.bandNames()
        agbd = img.select("agbd")
        qa_mask = ee.Image(
            ee.Algorithms.If(
                band_names.contains("QA"),
                img.select("QA").eq(1),
                ee.Image(1),
            )
        )
        return agbd.updateMask(qa_mask)

    img = collection.map(mask_valid).median()
    return img.multiply(0.47).rename("agb")


def _loader_spawn(entry, dataset_year, roi, unmask, ee):
    img = ee.ImageCollection(entry["gee_id"]).first()
    return img.select("agb").rename("agb")


def _loader_ornl_agb_bgb(entry, dataset_year, roi, unmask, ee):
    img = ee.ImageCollection(entry["gee_id"]).first()
    agb = img.select("agb")
    bgb = img.select("bgb")
    return agb.add(bgb).rename("agb")


def _loader_openlandmap_soc(entry, dataset_year, roi, unmask, ee):
    # b0 = surface (0 cm depth), unit is g/kg (not Mg C/ha)
    img = ee.Image(entry["gee_id"])
    return img.select("b0").rename("agb")


def _loader_esa_cci_satio_agb(entry, dataset_year, roi, unmask, ee):
    collection = ee.ImageCollection(entry["gee_id"])
    if roi:
        collection = collection.filterBounds(roi)
    year_col = collection.filterDate(
        f"{dataset_year}-01-01", f"{dataset_year}-12-31"
    )
    size = year_col.size().getInfo()
    img = year_col.first() if size > 0 else collection.sort("system:time_start", False).first()
    # Verify band name at runtime — community asset band names can vary
    band_names = img.bandNames().getInfo()
    logger.info(f"ESA_CCI_SATIO_AGB available bands: {band_names}")
    candidate = next((b for b in ("AGB", "agb", "AGB_mean") if b in band_names), None)
    if candidate is None:
        logger.warning(f"ESA_CCI_SATIO_AGB: unexpected bands {band_names}, selecting band 0")
        return img.select(0).multiply(0.47).rename("agb")
    return img.select(candidate).multiply(0.47).rename("agb")


def _loader_simard(entry, dataset_year, roi, unmask, ee):
    hansen = ee.Image(entry["gee_id"])
    return hansen.select("treecover2000").multiply(2.0).rename("agb")


_LOADERS = {
    "WCMC":               _loader_wcmc,
    "ESA_CCI":            _loader_esa_cci,
    "GEDI":               _loader_gedi,
    "GEDI_L4A_MONTHLY":   _loader_gedi_l4a_monthly,
    "GEDI_L4B_STACK":     _loader_gedi_l4b_stack,
    "GEDI_L4D":           _loader_gedi_l4d,
    "SPAWN":              _loader_spawn,
    "ORNL_AGB_BGB":       _loader_ornl_agb_bgb,
    "OPENLANDMAP_SOC":    _loader_openlandmap_soc,
    "ESA_CCI_SATIO_AGB":  _loader_esa_cci_satio_agb,
    "Simard":             _loader_simard,
}
