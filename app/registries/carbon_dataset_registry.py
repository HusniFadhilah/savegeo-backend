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
# provider_type = "external_raster" → stats via HTTP REST/WCS/COG,
# tiles NOT available in current implementation (explicit error returned).
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
        "name":             "GMW Mangrove AGB Carbon",
        "full_name":        "JAXA Global Mangrove Watch — Aboveground Biomass Carbon (2020, 25 m)",
        "service_url":      None,
        "source_url":       "https://www.eorc.jaxa.jp/ALOS/en/dataset/gmw_e.htm",
        "band":             "agb",
        "unit":             "Mg C/ha",
        "target_pool":      "mangrove_aboveground_biomass_carbon",
        "resolution":       25,
        "year":             2020,
        "year_range":       [1996, 2020],
        "time_aware":       False,
        "transform":        "multiply_0.47",  # Mg biomass/ha → Mg C/ha
        "description":      "JAXA Global Mangrove Watch 2020 AGB at 25 m. Mangrove/coastal ecosystems only — not valid for terrestrial forest carbon. Carbon = AGB × 0.47.",
        "attribution":      "JAXA / Global Mangrove Watch Consortium",
        "limitations":      [
            "Mangrove ecosystems only — results invalid for terrestrial forest areas.",
            "Point sampling requires rasterio + COG access; not available without optional dependency.",
            "No tile rendering without rasterio or GEE re-ingestion.",
        ],
        "vis_min":          0,
        "vis_max":          150,
        "vis_palette":      ["f7fcfd","e0ecf4","bfd3e6","9ebcda","8c96c6","88419d","6e016b"],
        "ingestion_method": "cog_rasterio",
        "training_capable": False,  # requires rasterio + COG; not bundled
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
    },
}
# fmt: on

# Keys exposed for training CLI choices (excludes deprecated Simard)
TRAINING_DATASET_KEYS: list[str] = [
    "WCMC",
    "ESA_CCI",
    "GEDI",
    "GEDI_L4A_MONTHLY",
    "GEDI_L4B_STACK",
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

    Supports datasets in CARBON_EXTERNAL_REGISTRY with ingestion_method='cloud_geotiff_ee'.
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

    if meta.get("ingestion_method") != "cloud_geotiff_ee":
        raise ValueError(
            f"Dataset '{key}' does not use cloud_geotiff_ee loading "
            f"(ingestion_method={meta.get('ingestion_method')!r}). "
            "Use ExternalRasterProvider for REST/rasterio-based datasets."
        )

    raster_url = (
        os.getenv(f"{key}_URL")
        or os.getenv(f"{key.upper()}_URL")
        or meta.get("raster_url")
    )
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

    if roi is not None:
        img = img.clip(roi)

    transform = meta.get("transform", "none")
    if transform == "multiply_0.47":
        img = img.multiply(0.47).rename("agb")
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
    "SPAWN":              _loader_spawn,
    "ORNL_AGB_BGB":       _loader_ornl_agb_bgb,
    "OPENLANDMAP_SOC":    _loader_openlandmap_soc,
    "ESA_CCI_SATIO_AGB":  _loader_esa_cci_satio_agb,
    "Simard":             _loader_simard,
}
