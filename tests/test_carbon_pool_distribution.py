from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from app.services import carbon_annual_store as store
from app.services import carbon_pool_service as service


def test_distribution_preserves_zero_bins_and_observed_quantiles():
    result = service._pool_density_statistics({"agb_mean": 5, "agb_stdDev": 2, "agb_min": 0, "agb_max": 10,
        "agb_p2": 0, "agb_p5": 0.5, "agb_p25": 2, "agb_p50": 5, "agb_p75": 8,
        "agb_p95": 9.5, "agb_p98": 10,
        "agb_histogram": {"bucketMeans": [0, 5, 10], "histogram": [2, 0, 3]}}, 1000, 250)
    assert result["histogram"] == {"bucket_means": [0, 5, 10], "counts": [2, 0, 3],
                                   "pixel_count": 5, "sampled": True, "scale_m": 1000}
    assert result["percentiles"] == {"p2": 0, "p5": 0.5, "p25": 2, "median": 5,
                                     "p75": 8, "p95": 9.5, "p98": 10}


def test_fallback_uses_actual_sample_values_including_constant_zero_raster():
    image = MagicMock()
    image.select.return_value.sample.return_value.getInfo.return_value = {
        "features": [{"properties": {"agb": value}} for value in [0, 0, 0, None, float("nan")]]}
    result = service._sample_pool_distribution(image, MagicMock(), 250)
    assert result["histogram"]["pixel_count"] == 3
    assert sum(result["histogram"]["counts"]) == 3
    assert result["percentiles"] == {"p25": 0, "median": 0, "p75": 0}


@pytest.mark.parametrize("dataset,pool", [("ORNL_BGB", "belowground_biomass_carbon"),
                                         ("SOILGRIDS_OCS_0_30CM", "soil_organic_carbon")])
def test_simple_pool_response_and_saved_summary_include_distribution(dataset, pool):
    model = {"name": "national_pool", "version": "1", "algorithm": "RF", "description": "annual",
             "metadata_json": {"method": "saved_reference_ensemble", "compatible_reference_datasets": [dataset]}}
    meta = {"year": 2020, "resolution": 250, "unit": "Mg C/ha", "target_pool": pool,
            "name": "reference", "full_name": "reference", "description": "baseline", "gee_id": "test"}
    raw = {"agb_mean": 10, "agb_stdDev": 3, "agb_min": 0, "agb_max": 20,
           "agb_p25": 5, "agb_p50": 10, "agb_p75": 15,
           "agb_histogram": {"bucketMeans": [0, 10, 20], "histogram": [2, 6, 2]},
           "valid_area_sum": 100, "aoi_area": 125, "carbon_total_sum": 1000}
    image = MagicMock()
    image.updateMask.return_value = image
    image.clip.return_value = image
    image.addBands.return_value = image
    image.reduceRegion.return_value.set.return_value.getInfo.return_value = raw
    image.getMapId.return_value = {"tile_fetcher": type("Tile", (), {"url_format": "fresh-map"})()}
    roi = MagicMock()
    roi.bounds.return_value.area.return_value.getInfo.return_value = 1e8
    data = {"aoi": {"west": 110, "south": -3, "east": 111, "north": -2}, "year": 2026,
            "dataset_year": 2020, "reference_dataset": dataset, "model_name": model["name"], "simple_mode": True}
    with patch.object(service, "ee") as ee, patch.object(service, "_geometry", return_value=roi), \
         patch.object(service, "get_dataset_meta", return_value=deepcopy(meta)), \
         patch.object(service, "load_carbon_reference_ee", return_value=image), \
         patch.object(service, "get_builtin_carbon_model", return_value=model), \
         patch.object(service.config_service, "get_analysis_defaults", return_value={"max_pixels": 1000000,
             "carbon_vis_min": 0, "carbon_vis_max": 100, "carbon_vis_palette": ["green"], "carbon_co2_factor": 3.67}), \
         patch("app.services.carbon_saved_models.saved_model_image", return_value=(image, {})), \
         patch.object(store, "load_summary", return_value=None), patch.object(store, "save_summary", return_value=True) as save:
        result = service.analyze_carbon_pool(MagicMock(), data)
        ee.Reducer.histogram.assert_called_once_with(32, None, 10000)
        ee.Reducer.percentile.assert_called_once_with([2, 5, 25, 50, 75, 95, 98])
    stats = result["carbon_estimated"]["statistics"]
    assert stats["histogram"]["counts"] == [2, 6, 2]
    assert stats["percentiles"]["median"] == 10
    assert result["area_info"]["total_carbon_tons"] == 1000
    assert result["data_quality"]["valid_pixel_pct"] == 80
    assert save.call_args.args[-1]["statistics"] == stats
