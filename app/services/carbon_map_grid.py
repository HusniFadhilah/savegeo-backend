"""Map-only projection helpers for carbon result layers."""

import math

WEB_MAP_CRS = "EPSG:3857"


def apply_carbon_map_grid(image, resolution_m: int):
    """Project a carbon display layer onto its reported web-map grid.

    Call this only after statistics have been calculated. Earth Engine tiles
    otherwise choose scale from the map zoom level, so a configured map grid
    would be metadata only. The projection is for display; it does not add
    source detail or alter the analysis/statistics scale.
    """
    if isinstance(resolution_m, bool) or not isinstance(resolution_m, (int, float)):
        return image
    if not math.isfinite(resolution_m) or resolution_m <= 0 or not float(resolution_m).is_integer():
        return image
    return image.reproject(crs=WEB_MAP_CRS, scale=int(resolution_m))
