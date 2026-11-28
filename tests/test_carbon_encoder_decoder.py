from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

try:
    import torch
except ImportError:  # optional model runtime
    torch = None

from app.inference.carbon_encoder_decoder import (
    CLASS_MAP,
    CLASS_ORDER,
    CarbonLandCoverUNet,
    CarbonStockRegressionUNet,
    SensorRaster,
    TargetGrid,
    align_sensor_rasters,
    carbon_regression_metrics,
    channel_order,
    class_area_statistics,
    convert_carbon_density,
    evaluate_spatial_segmentation_holdout,
    masked_carbon_regression_loss,
    segmentation_metrics,
    segmentation_prediction,
    spatial_block_holdout,
    train_encoder_decoder,
)


def _optical(*, cloud_mask=None, nodata_cell=False):
    values = np.ones((6, 4, 4), dtype=np.float32)
    if nodata_cell:
        values[:, 3, 3] = -9999
    return SensorRaster(
        data=values,
        band_names=("B2", "B3", "B4", "B8", "B11", "B12"),
        crs="EPSG:4326",
        transform=from_origin(0, 4, 1, 1),
        nodata=-9999,
        cloud_mask=cloud_mask,
        metadata={"collection": "COPERNICUS/S2_SR_HARMONIZED", "scale": 0.0001},
    )


def _radar():
    return SensorRaster(
        data=np.stack([np.full((2, 2), -10), np.full((2, 2), -15)]).astype(np.float32),
        band_names=("VV", "VH"),
        crs="EPSG:4326",
        transform=from_origin(0, 4, 2, 2),
        valid_mask=np.array([[True, True], [True, False]]),
        metadata={"collection": "COPERNICUS/S1_GRD"},
    )


def test_sensor_modes_have_explicit_channel_order_and_align_masks():
    target = TargetGrid("EPSG:4326", from_origin(0, 4, 1, 1), 4, 4, 1.0)
    cloud = np.zeros((4, 4), dtype=bool)
    cloud[0, 0] = True
    result = align_sensor_rasters(
        {"optical": _optical(cloud_mask=cloud, nodata_cell=True), "radar": _radar()},
        target,
        sensor_mode="optical_radar",
    )

    assert result["data"].shape == (9, 4, 4)
    assert tuple(result["band_order"]) == channel_order("optical_radar")
    assert result["valid_mask"].shape == (4, 4)
    assert not result["valid_mask"][0, 0]  # cloud mask retained
    assert not result["valid_mask"][3, 3]  # source no-data retained
    assert not result["valid_mask"][2, 2]  # radar no-data retained after nearest mask warp
    assert np.all(result["data"][:, ~result["valid_mask"]] == 0)
    assert result["provenance"]["transformations"][1]["resampling"] == "bilinear_values_nearest_mask"


def test_optical_only_and_radar_only_have_distinct_input_configs():
    target = TargetGrid("EPSG:4326", from_origin(0, 4, 1, 1), 4, 4)
    optical = align_sensor_rasters({"optical": _optical()}, target, sensor_mode="optical_only")
    radar = align_sensor_rasters({"radar": _radar()}, target, sensor_mode="radar_only")

    assert optical["data"].shape[0] == 6
    assert radar["band_order"] == ["VV", "VH", "VV_minus_VH"]
    assert radar["data"].shape[0] == 3


@pytest.mark.skipif(torch is None, reason="PyTorch is an optional model runtime")
def test_unet_encoder_decoder_shapes_and_confidence_mask():
    model = CarbonLandCoverUNet(input_channels=6, base_channels=8)
    input_tensor = torch.randn(1, 6, 17, 19)
    output = model(input_tensor)
    mask = torch.ones((17, 19), dtype=torch.bool)
    mask[0, 0] = False
    prediction = segmentation_prediction(model, input_tensor, mask)

    assert output.shape == (1, len(CLASS_ORDER), 17, 19)
    assert prediction["probabilities"].shape == (1, len(CLASS_ORDER), 17, 19)
    assert prediction["classes"][0, 0, 0].item() == -1
    assert prediction["confidence"][0, 0, 0].item() == 0.0
    assert torch.allclose(prediction["probabilities"][0, :, 1, 1].sum(), torch.tensor(1.0), atol=1e-5)
    assert set(CLASS_MAP) == set(CLASS_ORDER)


@pytest.mark.skipif(torch is None, reason="PyTorch is an optional model runtime")
def test_stock_regression_is_separate_continuous_unet_head():
    model = CarbonStockRegressionUNet(input_channels=9, base_channels=8)
    assert model(torch.randn(2, 9, 16, 16)).shape == (2, 1, 16, 16)


def test_spatial_holdout_keeps_whole_blocks_out_of_training():
    train, holdout = spatial_block_holdout(["west", "west", "east", "north"], ["north"])
    groups = np.array(["west", "west", "east", "north"])
    assert set(groups[train]).isdisjoint(set(groups[holdout]))
    assert groups[holdout].tolist() == ["north"]

    report = evaluate_spatial_segmentation_holdout(
        np.array([[0], [1], [1], [0]]), np.array([[0], [1], [0], [0]]),
        ["west", "west", "east", "north"], ["north"], class_count=2,
        label_source="licensed reference raster v1",
    )
    assert report["validation_type"] == "spatial_holdout_reference_raster"
    assert report["holdout_blocks"] == ["north"]
    assert report["independent_field_plot_validation"] is False


def test_segmentation_metrics_and_area_include_per_class_counts():
    metrics = segmentation_metrics(np.array([0, 0, 1, 1]), np.array([0, 1, 1, 1]), class_count=2)
    areas = class_area_statistics(np.array([[0, 1], [-1, 1]]), np.array([[1, 1], [1, 0]], dtype=bool), pixel_area_m2=100)

    assert metrics["sample_count"] == 4
    assert metrics["confusion_matrix"] == [[1, 1], [0, 2]]
    assert metrics["per_class"]["0"]["iou"] == 0.5
    assert metrics["miou"] == pytest.approx(7 / 12)
    assert areas["0"]["pixel_count"] == 1
    assert areas["0"]["area_ha"] == pytest.approx(0.01)


def test_regression_metrics_keep_unit_sample_count_and_negative_r2():
    metrics = carbon_regression_metrics(
        np.array([1.0, 2.0, 3.0]), np.array([10.0, 10.0, 10.0]),
        unit="Mg C/ha", source_target="field AGB plot measurements",
    )

    assert metrics["metric_type"] == "carbon_regression"
    assert metrics["sample_count"] == 3
    assert metrics["unit"] == "Mg C/ha"
    assert metrics["source_target"] == "field AGB plot measurements"
    assert metrics["r2"] < 0


def test_carbon_density_unit_conversion_does_not_guess_biomass_fraction():
    assert convert_carbon_density(1.0, source_unit="kg C/m2", target_unit="Mg C/ha") == pytest.approx(10.0)
    assert convert_carbon_density(1000.0, source_unit="kg C/ha", target_unit="Mg C/ha") == pytest.approx(1.0)
    with pytest.raises(ValueError, match="carbon fraction"):
        convert_carbon_density(1.0, source_unit="Mg biomass/ha", target_unit="Mg C/ha")


@pytest.mark.skipif(torch is None, reason="PyTorch is an optional model runtime")
def test_encoder_decoder_training_uses_task_specific_masked_targets():
    segmentation = CarbonLandCoverUNet(input_channels=4, base_channels=8)
    optimizer = torch.optim.Adam(segmentation.parameters(), lr=0.001)
    image = torch.rand(1, 4, 16, 16)
    labels = torch.randint(0, len(CLASS_ORDER), (1, 16, 16))
    valid = torch.ones((1, 16, 16), dtype=torch.bool)
    valid[:, 0, 0] = False
    history = train_encoder_decoder(segmentation, [(image, labels, valid)], optimizer, task="segmentation", epochs=1)
    assert len(history) == 1 and np.isfinite(history[0])

    prediction = torch.tensor([[[[1.0, 4.0]]]])
    observed = torch.tensor([[[[1.0, 2.0]]]])
    regression_mask = torch.tensor([[[[True, False]]]])
    assert masked_carbon_regression_loss(prediction, observed, regression_mask).item() == 0.0
