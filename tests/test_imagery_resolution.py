from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from rio_tiler.models import ImageData

from app.services import imagery_resolution, imagery_service
from app.registries.imagery_provider_registry import get_imagery_provider_meta, provider_capabilities


def test_sentinel_rgb_catalog_uses_harmonized_ten_metre_bands():
    meta = get_imagery_provider_meta("sentinel2")
    assert meta["gee_collection"] == "COPERNICUS/S2_SR_HARMONIZED"
    assert [meta["band_role_map"][role] for role in ("red", "green", "blue")] == ["B4", "B3", "B2"]
    assert meta["resolution_m"] == 10


def test_provider_capabilities_describe_single_band_and_stac_access():
    gas = provider_capabilities(get_imagery_provider_meta("sentinel5p_no2"))
    assert gas["supports_bands"] is True
    assert gas["supports_cloud_filter"] is False
    assert gas["analytical"] is True

    stac = provider_capabilities(get_imagery_provider_meta("stac_catalog"))
    assert stac["downloadable"] is True
    assert stac["supports_raw_data"] is True
    assert stac["visualization_only"] is False


def test_stac_never_selects_thumbnail_even_when_named_visual():
    assets = {"visual": {"href": "preview.jpg", "roles": ["visual"]},
              "rendered_preview": {"href": "preview.tif"},
              "analytic": {"href": "full.tif", "roles": ["data"]}}
    options = imagery_service._stac_asset_options("https://example.org/item", {"assets": assets})
    assert [asset["key"] for asset in options] == ["analytic"]
    assert imagery_service._default_stac_asset_key(options) == "analytic"
    with patch.object(imagery_service, "_fetch_json", return_value={"assets": assets}):
        with pytest.raises(imagery_service.AnalysisError, match="eksplisit"):
            imagery_service._stac_asset_href("https://example.org/missing-visual-test", "visual")


def test_median_preserves_each_band_grid_instead_of_a_coarse_first_band():
    collection = MagicMock()
    bands = ["B4", "B3", "B2", "B11"]
    sources = {band: MagicMock(name=band) for band in bands}
    medians = {band: MagicMock(name=f"median-{band}") for band in bands}
    collection.first.return_value.select.side_effect = sources.__getitem__
    collection.select.side_effect = lambda band: medians[band]
    with patch.object(imagery_resolution.ee.Image, "cat") as cat:
        imagery_resolution.native_median(collection, bands)
    for band in bands:
        medians[band].median.return_value.setDefaultProjection.assert_called_once_with(sources[band].projection.return_value)
    assert len(cat.call_args.args[0]) == 4


def test_optional_interpolation_never_invents_finer_native_resolution():
    image = MagicMock()
    unchanged, metadata = imagery_service._apply_super_resolution(image, {"resolution_m": 10}, "off")
    assert unchanged is image and metadata is None
    _, metadata = imagery_service._apply_super_resolution(image, {"resolution_m": 10}, "bicubic_4x")
    assert metadata["native_resolution_m"] == metadata["render_scale_m"] == 10
    assert metadata["factor"] == 1
    image.setDefaultProjection.assert_not_called()
    image.resample.return_value.setDefaultProjection.assert_not_called()


def test_rgb_tile_uses_final_masked_selected_clipped_image():
    image, clipped, aoi = MagicMock(), MagicMock(), MagicMock()
    for method in ("select", "multiply", "add"):
        getattr(image, method).return_value = image
    image.clip.return_value = clipped
    meta = get_imagery_provider_meta("sentinel2")
    with patch.object(imagery_service, "ee") as ee, \
         patch.object(imagery_service, "_apply_s2_single_scene_mask", return_value=image) as mask, \
         patch.object(imagery_service, "create_geometry_from_payload", return_value=aoi), \
         patch.object(imagery_service, "get_tile_url", return_value={"tile_url": "full-resolution-tiles"}) as tile:
        matches = ee.ImageCollection.return_value.filter.return_value
        matches.size.return_value.getInfo.return_value = 1
        matches.first.return_value = image
        result = imagery_service.get_scene_tile({"satellite": "sentinel2", "scene_id": "scene", "aoi": {"geojson": {}}, "cloud_mask_technique": "scl"})
    mask.assert_called_once_with(image, "scl")
    assert image.select.call_args_list[-1].args == (["B4", "B3", "B2"],)
    image.clip.assert_called_once_with(aoi)
    assert tile.call_args.args[0] is clipped
    assert tile.call_args.args[1]["bands"] == ["B4", "B3", "B2"]
    assert result["resolution_m"] == 10
    assert result["dataset"] == meta["gee_collection"]
    image.reproject.assert_not_called()


def test_large_sentinel_aoi_uses_date_range_mosaic():
    metadata = get_imagery_provider_meta("sentinel2")
    with patch.object(imagery_service, "ee") as ee, \
         patch.object(imagery_service, "create_geometry_from_payload", return_value=MagicMock(name="aoi")), \
         patch.object(imagery_service, "_aoi_exceeds_scene_footprint", return_value=True), \
         patch.object(imagery_service, "_get_sentinel2_mosaic_tile", return_value={
             "tile_url": "mosaic-url",
             "scene_count": 4,
             "super_resolution": None,
             "cloud_mask_technique": "scl",
         }) as mosaic:
        matches = ee.ImageCollection.return_value.filter.return_value
        matches.size.return_value.getInfo.return_value = 1
        result = imagery_service.get_scene_tile({
            "satellite": "sentinel2",
            "scene_id": "scene",
            "aoi": {"geojson": {}},
            "auto_mosaic": True,
            "start_date": "2025-01-01",
            "end_date": "2025-02-01",
            "cloud_mask_technique": "scl",
        })
    mosaic.assert_called_once()
    assert result["tile_url"] == "mosaic-url"
    assert result["render_mode"] == "mosaic"
    assert result["scene_count"] == 4
    assert result["aoi_larger_than_scene"] is True
    assert result["satellite"] == metadata


def test_cog_renderer_reads_requested_high_zoom_without_thumbnail_resize():
    with patch.object(imagery_service, "_readable_stac_asset_href", return_value="https://example.org/full.tif"), \
         patch.object(imagery_service, "_validate_public_http_url"), \
         patch.object(imagery_service, "Reader") as reader:
        reader.return_value.__enter__.return_value.tile.return_value = ImageData(np.ma.array(np.full((3, 256, 256), 123, dtype="uint8"), mask=False))
        imagery_service._render_cog_tile("https://example.org/item", 14, 12000, 8000, "visual", "1,2,3", None)
        image = reader.return_value.__enter__.return_value
        image.tile.assert_called_once_with(12000, 8000, 14, tilesize=256, indexes=[1, 2, 3])
        image.preview.assert_not_called()

