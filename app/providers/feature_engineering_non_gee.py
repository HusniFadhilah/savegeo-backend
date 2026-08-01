"""
feature_engineering_non_gee.py — numpy port of the spectral index / terrain /
interaction feature math used by the GEE pipeline, for use without ee.Image.

Index formulas here are an exact numeric replication of
inference/carbon_inference.py::_add_s2_indices() and
training/data_preparation.py::calculate_indices(). Any future change to those
GEE formulas should be mirrored here so non-GEE models stay comparable.

No GEE, no xarray/dask — plain numpy over in-memory band arrays produced by
providers/stac_provider.py or providers/local_raster_provider.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Matches CarbonInferenceEngine.STANDARD_S2_FEATURES band order exactly.
S2_BAND_ORDER: List[str] = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
INDEX_NAMES: List[str] = ["NDVI", "NDWI", "NDMI", "NBR", "NDRE", "EVI", "SAVI", "BSI", "brightness"]
STANDARD_S2_NON_GEE_FEATURES: List[str] = S2_BAND_ORDER + INDEX_NAMES

TERRAIN_NAMES: List[str] = ["elevation", "slope", "aspect"]
LANDCOVER_NAMES: List[str] = ["landcover", "forest_mask"]
INTERACTION_NAMES: List[str] = ["NDVI_x_elevation", "NDMI_x_slope", "forest_mask_x_NDVI", "B8_x_B11"]

FEATURE_STACKS: Dict[str, List[str]] = {
    "s2_bands_only_non_gee": list(S2_BAND_ORDER),
    "standard_s2_non_gee": STANDARD_S2_NON_GEE_FEATURES,
    "standard_s2_non_gee_dem_landcover": (
        STANDARD_S2_NON_GEE_FEATURES + TERRAIN_NAMES + LANDCOVER_NAMES + INTERACTION_NAMES
    ),
}


@dataclass
class NonGeeStack:
    """Common contract produced by both stac_provider and local_raster_provider.

    bands: dict of band-name -> 2D float array, all same shape, aligned to
           the same grid (same transform/crs/shape).
    """
    bands: Dict[str, np.ndarray]
    transform: object  # affine.Affine
    crs: object         # rasterio.crs.CRS
    shape: Tuple[int, int]  # (H, W)
    provenance: Dict = field(default_factory=dict)

    def pixel_size_m(self) -> float:
        """Approximate pixel size in meters from the affine transform.

        Assumes a projected CRS (meters); callers must reproject to a metric
        CRS (e.g. UTM) before building a NonGeeStack meant for terrain calc.
        """
        return float(abs(self.transform.a))


def compute_indices(bands: Dict[str, np.ndarray], prefix: str = "") -> Dict[str, np.ndarray]:
    """Exact numpy replication of the 9 GEE spectral index formulas.

    Expects reflectance-scale (0-1 float) bands B2,B3,B4,B5,B6,B7,B8,B8A,B11,B12.
    Division-by-zero pixels become NaN rather than raising or silently wrapping.
    """
    b2, b3, b4 = bands["B2"], bands["B3"], bands["B4"]
    b5, b8, b8a, b11, b12 = bands["B5"], bands["B8"], bands["B8A"], bands["B11"], bands["B12"]

    def _safe_nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = a + b
            out = np.where(denom == 0, np.nan, (a - b) / denom)
        return out

    indices: Dict[str, np.ndarray] = {}
    indices[f"{prefix}NDVI"] = _safe_nd(b8, b4)
    indices[f"{prefix}NDWI"] = _safe_nd(b3, b8)
    indices[f"{prefix}NDMI"] = _safe_nd(b8, b11)
    indices[f"{prefix}NBR"] = _safe_nd(b8, b12)
    indices[f"{prefix}NDRE"] = _safe_nd(b8a, b5)

    with np.errstate(divide="ignore", invalid="ignore"):
        evi_denom = b8 + 6 * b4 - 7.5 * b2 + 1
        indices[f"{prefix}EVI"] = np.where(
            evi_denom == 0, np.nan, 2.5 * (b8 - b4) / evi_denom
        )

        savi_denom = b8 + b4 + 0.5
        indices[f"{prefix}SAVI"] = np.where(
            savi_denom == 0, np.nan, ((b8 - b4) / savi_denom) * 1.5
        )

        bsi_denom = (b11 + b4) + (b8 + b2)
        indices[f"{prefix}BSI"] = np.where(
            bsi_denom == 0, np.nan, ((b11 + b4) - (b8 + b2)) / bsi_denom
        )

    indices[f"{prefix}brightness"] = np.nanmean(np.stack([b2, b3, b4]), axis=0)
    return indices


def compute_terrain(elevation: np.ndarray, pixel_size_m: float) -> Dict[str, np.ndarray]:
    """Slope (degrees) and aspect (degrees, 0-360, 0=N clockwise, GEE convention).

    Uses numpy.gradient central differences — a documented, dependency-free
    approximation of GEE's ee.Terrain.slope/aspect (which uses Horn's method).
    No richdem/gdaldem dependency added.
    """
    dz_dy, dz_dx = np.gradient(elevation, pixel_size_m)
    slope_rad = np.arctan(np.hypot(dz_dx, dz_dy))
    slope_deg = np.degrees(slope_rad)

    aspect_rad = np.arctan2(-dz_dx, dz_dy)
    aspect_deg = np.degrees(aspect_rad)
    aspect_deg = np.where(aspect_deg < 0, aspect_deg + 360, aspect_deg)

    return {"elevation": elevation, "slope": slope_deg, "aspect": aspect_deg}


def compute_interactions(
    bands: Dict[str, np.ndarray],
    indices: Dict[str, np.ndarray],
    terrain: Optional[Dict[str, np.ndarray]],
    forest_mask: Optional[np.ndarray],
) -> Dict[str, np.ndarray]:
    """NDVI x elevation, NDMI x slope, forest_mask x NDVI, B8 x B11.

    Any missing input (no terrain or no forest_mask) yields an all-zero array
    of the correct shape rather than raising, documented via a debug log.
    """
    shape = bands["B8"].shape
    ndvi = indices.get("NDVI", np.full(shape, np.nan))
    ndmi = indices.get("NDMI", np.full(shape, np.nan))

    if terrain is not None:
        elevation = terrain["elevation"]
        slope = terrain["slope"]
    else:
        logger.debug("compute_interactions: no terrain provided, elevation/slope interactions zeroed")
        elevation = np.zeros(shape)
        slope = np.zeros(shape)

    if forest_mask is None:
        logger.debug("compute_interactions: no forest_mask provided, forest_mask_x_NDVI zeroed")
        forest_mask = np.zeros(shape)

    return {
        "NDVI_x_elevation": ndvi * elevation,
        "NDMI_x_slope": ndmi * slope,
        "forest_mask_x_NDVI": forest_mask.astype(float) * ndvi,
        "B8_x_B11": bands["B8"] * bands["B11"],
    }


def compute_temporal_stats(band_timeseries: np.ndarray) -> Dict[str, np.ndarray]:
    """median/p25/p75/min/max along axis 0 of a (n_times, H, W) stack."""
    with np.errstate(invalid="ignore"):
        return {
            "median": np.nanmedian(band_timeseries, axis=0),
            "p25": np.nanpercentile(band_timeseries, 25, axis=0),
            "p75": np.nanpercentile(band_timeseries, 75, axis=0),
            "min": np.nanmin(band_timeseries, axis=0),
            "max": np.nanmax(band_timeseries, axis=0),
        }


def compute_seasonal_delta(
    stack_a: "NonGeeStack", stack_b: "NonGeeStack", index_name: str = "NDVI"
) -> np.ndarray:
    """Generic 'period A minus period B' delta for one index.

    NOT hardcoded dry/wet — caller supplies two already-built NonGeeStacks for
    whatever two date ranges they choose. See plan Section "open risks" point 2:
    a single nationwide dry/wet split does not generalize across Indonesia's
    climate zones, so this is deliberately generic and optional.
    """
    idx_a = compute_indices(stack_a.bands).get(index_name)
    idx_b = compute_indices(stack_b.bands).get(index_name)
    if idx_a is None or idx_b is None:
        raise ValueError(f"Index '{index_name}' not computable from given stacks")
    return idx_a - idx_b


def build_feature_stack(
    stack: "NonGeeStack",
    feature_stack_key: str,
) -> Tuple[np.ndarray, List[str]]:
    """Top-level dispatcher: NonGeeStack -> (feature_array[n_features,H,W], feature_names).

    feature_names ordering matches CarbonInferenceEngine.STANDARD_S2_FEATURES
    when feature_stack_key == "standard_s2_non_gee" (10 bands then 9 indices).
    """
    if feature_stack_key not in FEATURE_STACKS:
        raise ValueError(
            f"Unknown feature_stack_key '{feature_stack_key}'. "
            f"Available: {list(FEATURE_STACKS.keys())}"
        )

    feature_names = FEATURE_STACKS[feature_stack_key]
    indices = compute_indices(stack.bands)

    terrain = None
    if "elevation" in stack.bands:
        terrain = compute_terrain(stack.bands["elevation"], stack.pixel_size_m())
        if "slope" in stack.bands:
            terrain["slope"] = stack.bands["slope"]
        if "aspect" in stack.bands:
            terrain["aspect"] = stack.bands["aspect"]

    forest_mask = stack.bands.get("forest_mask")
    interactions = compute_interactions(stack.bands, indices, terrain, forest_mask)

    available: Dict[str, np.ndarray] = {}
    available.update(stack.bands)
    available.update(indices)
    if terrain is not None:
        available.update(terrain)
    available.update(interactions)

    missing = [f for f in feature_names if f not in available]
    if missing:
        raise ValueError(
            f"Feature stack '{feature_stack_key}' requires {missing} which are not "
            f"available in this NonGeeStack (bands present: {sorted(stack.bands.keys())}). "
            "Build the stack with include_dem/include_landcover as needed."
        )

    arrays = [available[f] for f in feature_names]
    return np.stack(arrays, axis=0), list(feature_names)


if __name__ == "__main__":
    # Smoke test: synthetic reflectance arrays, hand-verifiable index values.
    rng = np.random.default_rng(0)
    shape = (64, 64)
    bands = {
        b: np.clip(rng.normal(0.2, 0.05, shape), 0.001, 0.9).astype(np.float32)
        for b in S2_BAND_ORDER
    }
    # Hand-picked pixel: B8=0.5, B4=0.1 -> NDVI = (0.5-0.1)/(0.5+0.1) = 0.6667
    bands["B8"][0, 0] = 0.5
    bands["B4"][0, 0] = 0.1
    idx = compute_indices(bands)
    expected_ndvi = (0.5 - 0.1) / (0.5 + 0.1)
    actual_ndvi = idx["NDVI"][0, 0]
    assert np.isclose(actual_ndvi, expected_ndvi, atol=1e-6), (actual_ndvi, expected_ndvi)
    print(f"NDVI hand-check OK: {actual_ndvi:.4f} == {expected_ndvi:.4f}")

    # Division-by-zero case: B8=B4=0 at one pixel -> NDVI should be NaN, not crash.
    bands2 = {k: v.copy() for k, v in bands.items()}
    bands2["B8"][1, 1] = 0.0
    bands2["B4"][1, 1] = 0.0
    idx2 = compute_indices(bands2)
    assert np.isnan(idx2["NDVI"][1, 1]), "expected NaN at zero-denominator pixel"
    print("Zero-denominator NaN-safety check OK")

    elevation = rng.normal(500, 50, shape).astype(np.float32)
    terrain = compute_terrain(elevation, pixel_size_m=20.0)
    assert terrain["slope"].shape == shape and terrain["aspect"].shape == shape
    assert np.all(terrain["slope"] >= 0)
    assert np.all((terrain["aspect"] >= 0) & (terrain["aspect"] <= 360))
    print("Terrain slope/aspect range check OK")

    from dataclasses import is_dataclass
    assert is_dataclass(NonGeeStack)
    fake_stack = NonGeeStack(
        bands={**bands, "elevation": elevation},
        transform=type("A", (), {"a": 20.0})(),
        crs=None,
        shape=shape,
    )
    feats, names = build_feature_stack(fake_stack, "standard_s2_non_gee")
    assert feats.shape == (len(STANDARD_S2_NON_GEE_FEATURES),) + shape
    assert names == STANDARD_S2_NON_GEE_FEATURES
    print(f"build_feature_stack('standard_s2_non_gee') OK: {feats.shape}, {len(names)} features")
    print("All feature_engineering_non_gee.py smoke tests passed.")
