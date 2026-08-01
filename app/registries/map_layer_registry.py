"""
map_layer_registry.py — Configurable map layer registry.

Single source of truth for map layers served to the frontend:
basemaps, analysis layers, reference layers, and overlays.
Each layer carries display metadata so the frontend can build
the layer control, legend, and opacity slider without hardcoding.

Endpoint consumers:
    GET /api/basemaps           → get_basemaps()
    GET /api/map-layers         → get_all_layers()
    GET /api/map-layers?module= → get_all_layers(module=)
"""
from typing import Dict, List, Optional

TYPE_BASEMAP   = "basemap"
TYPE_ANALYSIS  = "analysis"
TYPE_REFERENCE = "reference"
TYPE_OVERLAY   = "overlay"

LEGEND_GRADIENT = "gradient"
LEGEND_CLASSES  = "classes"
LEGEND_NONE     = "none"

# fmt: off
MAP_LAYER_REGISTRY: List[Dict] = [

    # ── Basemaps ──────────────────────────────────────────────────────
    # Satellite is intentionally `order=1` (default basemap) — the frontend
    # picks the lowest-`order` enabled basemap as its initial layer.
    {
        "key":             "satellite",
        "name":            "Satellite",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "Esri",
        "source":          "ArcGIS World Imagery",
        "tile_url":        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution":     "Tiles &copy; Esri, Maxar, Earthstar Geographics",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        19,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           1,
        "is_default":      True,
    },
    {
        "key":             "roads",
        "name":            "Roads",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "OpenStreetMap",
        "source":          "openstreetmap.org",
        "tile_url":        "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution":     "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        19,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           2,
    },
    {
        "key":             "topo",
        "name":            "Topographic",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "OpenTopoMap",
        "source":          "opentopomap.org",
        "tile_url":        "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution":     "Map data &copy; OpenStreetMap contributors, SRTM | OpenTopoMap",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        17,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           3,
    },
    {
        "key":             "terrain",
        "name":            "Terrain Relief",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "Esri",
        "source":          "ArcGIS World Shaded Relief",
        "tile_url":        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Shaded_Relief/MapServer/tile/{z}/{y}/{x}",
        "attribution":     "Tiles &copy; Esri",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        13,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           4,
    },
    {
        "key":             "dark",
        "name":            "Dark",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "CARTO",
        "source":          "CartoDB Dark Matter",
        "tile_url":        "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "attribution":     "&copy; OpenStreetMap contributors &copy; CARTO",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        19,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           5,
    },
    {
        "key":             "light",
        "name":            "Light",
        "type":            TYPE_BASEMAP,
        "module":          None,
        "provider":        "CARTO",
        "source":          "CartoDB Positron",
        "tile_url":        "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        "attribution":     "&copy; OpenStreetMap contributors &copy; CARTO",
        "default_opacity": 1.0,
        "min_zoom":        1,
        "max_zoom":        19,
        "legend_mode":     LEGEND_NONE,
        "vis_params":      {},
        "unit":            None,
        "enabled":         True,
        "order":           6,
    },

    # ── Carbon — analysis layer (tile_url is dynamic, set per GEE response) ──
    {
        "key":             "carbon_estimated",
        "name":            "Estimated Carbon Stock",
        "type":            TYPE_ANALYSIS,
        "module":          "carbon",
        "provider":        "GEE",
        "source":          "Google Earth Engine — custom carbon model inference",
        "tile_url":        None,
        "attribution":     "&copy; Google Earth Engine",
        "default_opacity": 0.85,
        "min_zoom":        1,
        "max_zoom":        18,
        "legend_mode":     LEGEND_GRADIENT,
        "vis_params": {
            "min":     0,
            "max":     200,
            "palette": ["440154", "414487", "2a788e", "22a884", "7ad151", "fde725"],
        },
        "unit":    "Mg C/ha",
        "enabled": True,
        "order":   10,
    },

    # ── Carbon — reference layer (tile_url is dynamic, name/unit from carbon_reference) ──
    {
        "key":             "carbon_reference",
        "name":            "Reference Carbon Dataset",
        "type":            TYPE_REFERENCE,
        "module":          "carbon",
        "provider":        "GEE",
        "source":          "Google Earth Engine — carbon reference dataset",
        "tile_url":        None,
        "attribution":     "&copy; Google Earth Engine",
        "default_opacity": 0.75,
        "min_zoom":        1,
        "max_zoom":        18,
        "legend_mode":     LEGEND_GRADIENT,
        "vis_params": {
            "min":     0,
            "max":     200,
            "palette": ["440154", "414487", "2a788e", "22a884", "7ad151", "fde725"],
        },
        "unit":    "Mg C/ha",
        "enabled": True,
        "order":   11,
    },
]
# fmt: on


def get_all_layers(
    module: Optional[str] = None,
    enabled_only: bool = True,
) -> List[Dict]:
    layers = list(MAP_LAYER_REGISTRY)
    if enabled_only:
        layers = [l for l in layers if l.get("enabled", True)]
    if module is not None:
        layers = [l for l in layers if l.get("module") == module]
    return sorted(layers, key=lambda l: l.get("order", 999))


def get_basemaps(enabled_only: bool = True) -> List[Dict]:
    layers = [l for l in MAP_LAYER_REGISTRY if l["type"] == TYPE_BASEMAP]
    if enabled_only:
        layers = [l for l in layers if l.get("enabled", True)]
    return sorted(layers, key=lambda l: l.get("order", 999))


def get_layer(key: str) -> Optional[Dict]:
    for layer in MAP_LAYER_REGISTRY:
        if layer["key"] == key:
            return dict(layer)
    return None
