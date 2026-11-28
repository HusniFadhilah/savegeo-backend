"""Experimental, separate U-Net pipelines for land-cover and carbon regression.

This module defines architectures and data/evaluation contracts only. It does
not bundle weights, labels, or a production release. Optical/radar inputs are
aligned to one declared grid before channel stacking; no-data masks remain
separate from model values.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Literal

import numpy as np
from rasterio.enums import Resampling
from rasterio.warp import reproject

try:  # torch is an optional backend extra, not needed for API startup.
    import torch
    from torch import nn
    from torch.nn import functional as F
except ImportError:  # pragma: no cover - exercised in minimal backend installs
    torch = None
    nn = None
    F = None

try:
    from app.registries.landcover_dataset_registry import LAND_COVER_LEGENDS
except Exception:  # pragma: no cover - static fallback keeps training utilities importable
    LAND_COVER_LEGENDS = {
        "Dynamic_World": {
            "0": {"label": "Water", "color": "#419BDF"},
            "1": {"label": "Trees", "color": "#397D49"},
            "2": {"label": "Grass", "color": "#88B053"},
            "3": {"label": "Flooded vegetation", "color": "#7A87C6"},
            "4": {"label": "Crops", "color": "#E49635"},
            "5": {"label": "Shrub & Scrub", "color": "#DFC35A"},
            "6": {"label": "Built Area", "color": "#C4281B"},
            "7": {"label": "Bare ground", "color": "#A59B8F"},
            "8": {"label": "Snow & Ice", "color": "#B39FE1"},
        }
    }


CLASS_SCHEMA = "DYNAMIC_WORLD_9_CLASS"
CLASS_SCHEMA_VERSION = "1"
CLASS_MAP: dict[str, dict[str, str]] = {
    str(code): {"label": value["label"], "color": value["color"]}
    for code, value in LAND_COVER_LEGENDS["Dynamic_World"].items()
}
CLASS_ORDER = tuple(sorted(CLASS_MAP, key=int))
OPTICAL_BANDS = {
    "sentinel2": ("B2", "B3", "B4", "B8", "B11", "B12"),
    "landsat89": ("SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"),
}
RADAR_BANDS = ("VV", "VH")
RADAR_DERIVED_BANDS = ("VV_minus_VH",)
SENSOR_MODES = ("optical_only", "radar_only", "optical_radar")


@dataclass(frozen=True)
class SensorRaster:
    data: np.ndarray
    band_names: tuple[str, ...]
    crs: str
    transform: Any
    nodata: float | None = None
    valid_mask: np.ndarray | None = None
    cloud_mask: np.ndarray | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class TargetGrid:
    crs: str
    transform: Any
    width: int
    height: int
    resolution_m: float | None = None


def _require_torch() -> None:
    if torch is None or nn is None or F is None:
        raise RuntimeError("PyTorch is required; install SaveGeo's optional granite-biomass or segmentation extra.")


if nn is not None:
    class _ConvBlock(nn.Module):
        def __init__(self, input_channels: int, output_channels: int):
            super().__init__()
            groups = min(8, output_channels)
            while output_channels % groups:
                groups -= 1
            self.layers = nn.Sequential(
                nn.Conv2d(input_channels, output_channels, kernel_size=3, padding=1, bias=False),
                nn.GroupNorm(groups, output_channels),
                nn.GELU(),
                nn.Conv2d(output_channels, output_channels, kernel_size=3, padding=1, bias=False),
                nn.GroupNorm(groups, output_channels),
                nn.GELU(),
            )

        def forward(self, value):
            return self.layers(value)


    class _UNetBackbone(nn.Module):
        def __init__(self, input_channels: int, output_channels: int, base_channels: int = 32):
            super().__init__()
            widths = [base_channels, base_channels * 2, base_channels * 4, base_channels * 8]
            self.encoder1 = _ConvBlock(input_channels, widths[0])
            self.encoder2 = _ConvBlock(widths[0], widths[1])
            self.encoder3 = _ConvBlock(widths[1], widths[2])
            self.encoder4 = _ConvBlock(widths[2], widths[3])
            self.pool = nn.MaxPool2d(2)
            self.bottleneck = _ConvBlock(widths[3], widths[3] * 2)
            self.up4 = nn.ConvTranspose2d(widths[3] * 2, widths[3], 2, stride=2)
            self.decode4 = _ConvBlock(widths[3] * 2, widths[3])
            self.up3 = nn.ConvTranspose2d(widths[3], widths[2], 2, stride=2)
            self.decode3 = _ConvBlock(widths[2] * 2, widths[2])
            self.up2 = nn.ConvTranspose2d(widths[2], widths[1], 2, stride=2)
            self.decode2 = _ConvBlock(widths[1] * 2, widths[1])
            self.up1 = nn.ConvTranspose2d(widths[1], widths[0], 2, stride=2)
            self.decode1 = _ConvBlock(widths[0] * 2, widths[0])
            self.output = nn.Conv2d(widths[0], output_channels, kernel_size=1)

        @staticmethod
        def _up_to(value, skip):
            value = F.interpolate(value, size=skip.shape[-2:], mode="bilinear", align_corners=False)
            return value

        def forward(self, value):
            enc1 = self.encoder1(value)
            enc2 = self.encoder2(self.pool(enc1))
            enc3 = self.encoder3(self.pool(enc2))
            enc4 = self.encoder4(self.pool(enc3))
            center = self.bottleneck(self.pool(enc4))
            dec4 = self.decode4(torch.cat([self._up_to(self.up4(center), enc4), enc4], dim=1))
            dec3 = self.decode3(torch.cat([self._up_to(self.up3(dec4), enc3), enc3], dim=1))
            dec2 = self.decode2(torch.cat([self._up_to(self.up2(dec3), enc2), enc2], dim=1))
            dec1 = self.decode1(torch.cat([self._up_to(self.up1(dec2), enc1), enc1], dim=1))
            return self.output(dec1)


    class CarbonLandCoverUNet(nn.Module):
        """Nine-class semantic segmentation head using SaveGeo's Dynamic World schema."""

        def __init__(self, input_channels: int, class_count: int = len(CLASS_ORDER), base_channels: int = 32):
            super().__init__()
            if input_channels < 1 or class_count != len(CLASS_ORDER):
                raise ValueError("Input channels must be positive and class_count must match SaveGeo's class schema.")
            self.input_channels = input_channels
            self.class_count = class_count
            self.backbone = _UNetBackbone(input_channels, class_count, base_channels)

        def forward(self, value):
            if value.ndim != 4 or value.shape[1] != self.input_channels:
                raise ValueError(f"Expected NCHW input with {self.input_channels} channels.")
            return self.backbone(value)


    class CarbonStockRegressionUNet(nn.Module):
        """Separate continuous carbon-density regression head; never consumes class labels as targets."""

        def __init__(self, input_channels: int, base_channels: int = 32):
            super().__init__()
            if input_channels < 1:
                raise ValueError("Input channels must be positive.")
            self.input_channels = input_channels
            self.backbone = _UNetBackbone(input_channels, 1, base_channels)

        def forward(self, value):
            if value.ndim != 4 or value.shape[1] != self.input_channels:
                raise ValueError(f"Expected NCHW input with {self.input_channels} channels.")
            return self.backbone(value)
else:  # pragma: no cover - run when optional torch dependency is absent
    class CarbonLandCoverUNet:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs):
            _require_torch()

    class CarbonStockRegressionUNet:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs):
            _require_torch()


def channel_order(mode: Literal["optical_only", "radar_only", "optical_radar"], optical_sensor: str = "sentinel2") -> tuple[str, ...]:
    if mode not in SENSOR_MODES:
        raise ValueError(f"Unsupported sensor mode: {mode}")
    optical_bands = OPTICAL_BANDS.get(optical_sensor)
    if mode != "radar_only" and optical_bands is None:
        raise ValueError(f"Unsupported optical sensor: {optical_sensor}")
    if mode == "optical_only":
        return tuple(optical_bands or ())
    if mode == "radar_only":
        return (*RADAR_BANDS, *RADAR_DERIVED_BANDS)
    return (*tuple(optical_bands or ()), *RADAR_BANDS, *RADAR_DERIVED_BANDS)


def _source_valid_mask(source: SensorRaster) -> np.ndarray:
    array = np.asarray(source.data)
    if array.ndim != 3 or array.shape[0] != len(source.band_names):
        raise ValueError("Sensor raster data must be CxHxW and match the explicit band order.")
    valid = np.isfinite(array).all(axis=0)
    if source.nodata is not None and math.isfinite(float(source.nodata)):
        valid &= ~(array == source.nodata).any(axis=0)
    if source.valid_mask is not None:
        mask = np.asarray(source.valid_mask, dtype=bool)
        if mask.shape != valid.shape:
            raise ValueError("valid_mask shape must match the sensor raster grid.")
        valid &= mask
    if source.cloud_mask is not None:
        cloud = np.asarray(source.cloud_mask, dtype=bool)
        if cloud.shape != valid.shape:
            raise ValueError("cloud_mask shape must match the optical raster grid.")
        valid &= ~cloud
    return valid


def _warp_sensor(source: SensorRaster, target: TargetGrid) -> tuple[np.ndarray, np.ndarray]:
    data = np.asarray(source.data, dtype=np.float32)
    source_mask = _source_valid_mask(source)
    height, width = target.height, target.width
    warped = np.full((data.shape[0], height, width), np.nan, dtype=np.float32)
    for index, band in enumerate(data):
        source_band = band.copy()
        source_band[~source_mask] = np.nan
        reproject(
            source=source_band,
            destination=warped[index],
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=target.transform,
            dst_crs=target.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    mask_destination = np.zeros((height, width), dtype=np.uint8)
    reproject(
        source=source_mask.astype(np.uint8),
        destination=mask_destination,
        src_transform=source.transform,
        src_crs=source.crs,
        dst_transform=target.transform,
        dst_crs=target.crs,
        resampling=Resampling.nearest,
    )
    valid = mask_destination.astype(bool) & np.isfinite(warped).all(axis=0)
    return warped, valid


def _scale_sensor(source: SensorRaster) -> tuple[SensorRaster, dict[str, Any], list[str]]:
    metadata = source.metadata or {}
    scale = metadata.get("scale_factor")
    offset = metadata.get("add_offset", 0.0)
    steps: dict[str, Any] = {"scaling_applied": False, "source_units": metadata.get("units")}
    warnings = []
    data = np.asarray(source.data, dtype=np.float32)
    if scale is not None:
        if not isinstance(scale, (int, float)) or not math.isfinite(float(scale)):
            raise ValueError("Source scale_factor must be a finite number when provided.")
        if not isinstance(offset, (int, float)) or not math.isfinite(float(offset)):
            raise ValueError("Source add_offset must be a finite number when provided.")
        data = data * float(scale) + float(offset)
        steps = {"scaling_applied": True, "scale_factor": float(scale), "add_offset": float(offset), "source_units": metadata.get("units")}
    elif metadata.get("units") is None:
        warnings.append(f"{source.band_names[0] if source.band_names else 'sensor'}_value_units_missing")
    else:
        steps["scaling_note"] = "Input values were already in provider-declared units; SaveGeo applied no additional scale."
    return SensorRaster(
        data=data, band_names=source.band_names, crs=source.crs, transform=source.transform,
        nodata=source.nodata, valid_mask=source.valid_mask, cloud_mask=source.cloud_mask,
        metadata=metadata,
    ), steps, warnings


def align_sensor_rasters(
    inputs: dict[str, SensorRaster],
    target: TargetGrid,
    *,
    sensor_mode: Literal["optical_only", "radar_only", "optical_radar"],
    optical_sensor: str = "sentinel2",
) -> dict[str, Any]:
    """Warp optical and/or radar to one declared grid, then apply common no-data mask."""
    required = {"optical", "radar"} if sensor_mode == "optical_radar" else {"radar"} if sensor_mode == "radar_only" else {"optical"}
    if set(inputs) != required:
        raise ValueError(f"{sensor_mode} requires exactly these sensor inputs: {sorted(required)}")
    expected_optical = tuple(OPTICAL_BANDS.get(optical_sensor, ()))
    if sensor_mode != "radar_only" and inputs["optical"].band_names != expected_optical:
        raise ValueError(f"Optical band order must be {expected_optical} for {optical_sensor}.")
    if sensor_mode != "optical_only" and inputs["radar"].band_names != RADAR_BANDS:
        raise ValueError(f"Radar band order must be {RADAR_BANDS}.")

    warped: dict[str, np.ndarray] = {}
    valid: dict[str, np.ndarray] = {}
    transforms: list[dict[str, Any]] = []
    warnings: list[str] = []
    scaled_sources: dict[str, SensorRaster] = {}
    for sensor in ("optical", "radar"):
        if sensor not in inputs:
            continue
        original = inputs[sensor]
        raster, scaling, scale_warnings = _scale_sensor(original)
        warnings.extend(f"{sensor}_{warning}" for warning in scale_warnings)
        scaled_sources[sensor] = raster
        if sensor == "optical" and raster.cloud_mask is None:
            warnings.append("optical_cloud_mask_missing")
        metadata = raster.metadata or {}
        for field in ("provider", "mission", "product", "product_version", "native_resolution_m", "analysis_start_date", "analysis_end_date"):
            if metadata.get(field) is None:
                warnings.append(f"{sensor}_{field}_missing")
        warped[sensor], valid[sensor] = _warp_sensor(raster, target)
        transforms.append({
            "sensor": sensor,
            "source_crs": raster.crs,
            "source_transform": tuple(raster.transform),
            "source_bands": list(raster.band_names),
            "target_crs": target.crs,
            "target_transform": tuple(target.transform),
            "target_resolution_m": target.resolution_m,
            "resampling": "bilinear_values_nearest_mask",
            "scaling": scaling,
            "source_metadata": raster.metadata or {},
        })
    if sensor_mode == "optical_radar":
        optical_meta = scaled_sources["optical"].metadata or {}
        radar_meta = scaled_sources["radar"].metadata or {}
        optical_window = (optical_meta.get("analysis_start_date"), optical_meta.get("analysis_end_date"))
        radar_window = (radar_meta.get("analysis_start_date"), radar_meta.get("analysis_end_date"))
        if None not in optical_window + radar_window and optical_window != radar_window:
            warnings.append("sensor_analysis_windows_differ")
    valid_mask = np.logical_and.reduce(list(valid.values()))
    arrays: list[np.ndarray] = []
    names: list[str] = []
    if "optical" in warped:
        arrays.append(warped["optical"])
        names.extend(inputs["optical"].band_names)
    if "radar" in warped:
        radar = warped["radar"]
        arrays.extend((radar, (radar[0:1] - radar[1:2])))
        names.extend((*RADAR_BANDS, *RADAR_DERIVED_BANDS))
    stack = np.concatenate(arrays, axis=0).astype(np.float32, copy=False)
    stack[:, ~valid_mask] = 0.0
    return {
        "data": stack,
        "band_order": names,
        "valid_mask": valid_mask,
        "quality": {
            "valid_pixel_count": int(valid_mask.sum()),
            "total_pixel_count": int(valid_mask.size),
            "valid_pixel_pct": float(valid_mask.mean() * 100.0) if valid_mask.size else None,
            "no_data_policy": "intersection_of_selected_sensor_masks",
            "cloud_mask_applied": "optical cloud_mask removed before alignment" if "optical" in inputs and inputs["optical"].cloud_mask is not None else "optical_cloud_mask_missing" if "optical" in inputs else "not_applicable_radar_only",
            "warnings": sorted(set(warnings)),
        },
        "provenance": {
            "sensor_mode": sensor_mode,
            "optical_sensor": optical_sensor if "optical" in inputs else None,
            "target_crs": target.crs,
            "target_transform": tuple(target.transform),
            "target_shape_hw": [target.height, target.width],
            "band_order": names,
            "transformations": transforms,
            "radar_derived_channels": ["VV_minus_VH"] if "radar" in inputs else [],
        },
    }


def spatial_block_holdout(block_ids: Iterable[str | int], held_out_blocks: Iterable[str | int]) -> tuple[np.ndarray, np.ndarray]:
    """Split patches by whole spatial block IDs so a location cannot leak across sets."""
    groups = np.asarray(list(block_ids), dtype=object)
    held = {str(value) for value in held_out_blocks}
    if groups.ndim != 1 or not len(groups) or not held:
        raise ValueError("Spatial holdout needs patch block IDs and at least one held-out block.")
    normalized = np.asarray([str(value) for value in groups], dtype=object)
    test = np.flatnonzero(np.isin(normalized, list(held)))
    train = np.flatnonzero(~np.isin(normalized, list(held)))
    if not len(train) or not len(test):
        raise ValueError("Spatial holdout must leave non-empty train and test blocks.")
    train_groups, test_groups = set(normalized[train]), set(normalized[test])
    if train_groups & test_groups:
        raise ValueError("Spatial leakage: a block appears in both train and holdout sets.")
    return train, test


def evaluate_spatial_segmentation_holdout(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    block_ids: Iterable[str | int],
    held_out_blocks: Iterable[str | int],
    *,
    class_count: int,
    valid_mask: np.ndarray | None = None,
    label_source: str,
) -> dict[str, Any]:
    truth, prediction = np.asarray(y_true), np.asarray(y_pred)
    blocks = list(block_ids)
    if truth.shape != prediction.shape or truth.shape[0] != len(blocks) or truth.ndim < 2:
        raise ValueError("Spatial evaluation expects matching patch arrays and one block ID per patch.")
    _train_indices, holdout_indices = spatial_block_holdout(blocks, held_out_blocks)
    holdout_mask = None if valid_mask is None else np.asarray(valid_mask)[holdout_indices]
    metrics = segmentation_metrics(
        truth[holdout_indices], prediction[holdout_indices],
        class_count=class_count, valid_mask=holdout_mask,
    )
    return {
        **metrics,
        "validation_type": "spatial_holdout_reference_raster",
        "label_source": label_source,
        "holdout_scheme": "whole_spatial_blocks",
        "holdout_blocks": sorted({str(blocks[index]) for index in holdout_indices}),
        "holdout_patch_count": int(len(holdout_indices)),
        "independent_field_plot_validation": False,
    }


def segmentation_metrics(y_true: np.ndarray, y_pred: np.ndarray, *, class_count: int, valid_mask: np.ndarray | None = None) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=np.int64).reshape(-1)
    prediction = np.asarray(y_pred, dtype=np.int64).reshape(-1)
    if truth.shape != prediction.shape or class_count < 2:
        raise ValueError("Segmentation labels must have matching shapes and at least two classes.")
    valid = (truth >= 0) & (truth < class_count) & (prediction >= 0) & (prediction < class_count)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool).reshape(-1)
        if mask.shape != valid.shape:
            raise ValueError("valid_mask must match the label shape.")
        valid &= mask
    confusion = np.bincount(class_count * truth[valid] + prediction[valid], minlength=class_count**2).reshape(class_count, class_count)
    tp = np.diag(confusion).astype(float)
    actual = confusion.sum(axis=1).astype(float)
    predicted = confusion.sum(axis=0).astype(float)
    union = actual + predicted - tp
    precision = np.divide(tp, predicted, out=np.full(class_count, np.nan), where=predicted > 0)
    recall = np.divide(tp, actual, out=np.full(class_count, np.nan), where=actual > 0)
    f1 = np.divide(2 * precision * recall, precision + recall, out=np.full(class_count, np.nan), where=(precision + recall) > 0)
    iou = np.divide(tp, union, out=np.full(class_count, np.nan), where=union > 0)
    finite_iou = iou[np.isfinite(iou)]
    return {
        "metric_type": "reference_label_segmentation",
        "sample_count": int(valid.sum()),
        "confusion_matrix": confusion.tolist(),
        "per_class": {
            str(code): {
                "pixel_count": int(actual[code]),
                "predicted_count": int(predicted[code]),
                "precision": None if not np.isfinite(precision[code]) else float(precision[code]),
                "recall": None if not np.isfinite(recall[code]) else float(recall[code]),
                "f1": None if not np.isfinite(f1[code]) else float(f1[code]),
                "iou": None if not np.isfinite(iou[code]) else float(iou[code]),
            }
            for code in range(class_count)
        },
        "miou": float(finite_iou.mean()) if len(finite_iou) else None,
    }


def carbon_regression_metrics(y_true: np.ndarray, y_pred: np.ndarray, *, unit: str, source_target: str, valid_mask: np.ndarray | None = None) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=np.float64).reshape(-1)
    prediction = np.asarray(y_pred, dtype=np.float64).reshape(-1)
    if truth.shape != prediction.shape:
        raise ValueError("Carbon regression arrays must have matching shapes.")
    valid = np.isfinite(truth) & np.isfinite(prediction)
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool).reshape(-1)
        if mask.shape != valid.shape:
            raise ValueError("valid_mask must match carbon target shape.")
        valid &= mask
    if not valid.any():
        return {"metric_type": "carbon_regression", "sample_count": 0, "unit": unit, "source_target": source_target,
                "rmse": None, "mae": None, "bias": None, "r2": None}
    actual, predicted = truth[valid], prediction[valid]
    residual = predicted - actual
    total = float(np.square(actual - actual.mean()).sum())
    r2 = 1.0 - float(np.square(residual).sum()) / total if len(actual) > 1 and total > 0 else None
    return {
        "metric_type": "carbon_regression",
        "sample_count": int(valid.sum()),
        "unit": unit,
        "source_target": source_target,
        "rmse": float(np.sqrt(np.mean(np.square(residual)))),
        "mae": float(np.mean(np.abs(residual))),
        "bias": float(np.mean(residual)),
        "r2": r2,
    }


def convert_carbon_density(values: np.ndarray | float, *, source_unit: str, target_unit: str) -> np.ndarray | float:
    """Convert explicit carbon-density units only; never infer biomass-to-carbon fractions."""
    to_mg_c_ha = {
        "Mg C/ha": 1.0,
        "t C/ha": 1.0,
        "kg C/ha": 0.001,
        "g C/m2": 0.01,
        "kg C/m2": 10.0,
    }
    if source_unit not in to_mg_c_ha or target_unit not in to_mg_c_ha:
        raise ValueError("Carbon density conversion requires a supported carbon unit; biomass units need a documented carbon fraction.")
    array = np.asarray(values, dtype=np.float64)
    converted = array * to_mg_c_ha[source_unit] / to_mg_c_ha[target_unit]
    if not np.isfinite(converted).all():
        raise ValueError("Carbon density values must be finite.")
    return float(converted) if np.ndim(values) == 0 else converted


def class_area_statistics(classes: np.ndarray, valid_mask: np.ndarray, *, pixel_area_m2: float) -> dict[str, Any]:
    class_array = np.asarray(classes, dtype=np.int64)
    mask = np.asarray(valid_mask, dtype=bool)
    if class_array.shape != mask.shape or not math.isfinite(pixel_area_m2) or pixel_area_m2 <= 0:
        raise ValueError("Class raster, validity mask, and positive pixel area are required.")
    accepted = mask & (class_array >= 0) & (class_array < len(CLASS_ORDER))
    counts = np.bincount(class_array[accepted], minlength=len(CLASS_ORDER))
    return {
        str(class_index): {
            **CLASS_MAP[str(class_index)],
            "pixel_count": int(counts[class_index]),
            "area_ha": float(counts[class_index] * pixel_area_m2 / 10_000),
        }
        for class_index in range(len(CLASS_ORDER))
    }


def segmentation_prediction(model: Any, tensor: Any, valid_mask: Any) -> dict[str, Any]:
    """Return class probabilities, top-1 class, and confidence with no-data marked."""
    _require_torch()
    model.eval()
    with torch.no_grad():
        logits = model(tensor)
        probability = torch.softmax(logits, dim=1)
        confidence, classes = probability.max(dim=1)
        mask = torch.as_tensor(valid_mask, dtype=torch.bool, device=classes.device)
        if mask.ndim == 2:
            mask = mask.unsqueeze(0)
        if mask.shape != classes.shape:
            raise ValueError("valid_mask must match model output N,H,W shape.")
        classes = classes.masked_fill(~mask, -1)
        confidence = confidence.masked_fill(~mask, 0.0)
        probability = probability * mask.unsqueeze(1)
    return {"classes": classes, "probabilities": probability, "confidence": confidence, "valid_mask": mask}


def masked_segmentation_loss(logits: Any, target: Any, valid_mask: Any) -> Any:
    """Cross-entropy over labeled, valid pixels only; class labels are never carbon targets."""
    _require_torch()
    mask = torch.as_tensor(valid_mask, dtype=torch.bool, device=logits.device)
    labels = torch.as_tensor(target, dtype=torch.long, device=logits.device)
    if mask.shape != labels.shape:
        raise ValueError("Training mask and class label tensor must have equal shapes.")
    if not mask.any():
        raise ValueError("Training patch has no valid labeled pixels.")
    loss = F.cross_entropy(logits, labels, reduction="none")
    return loss[mask].mean()


def masked_carbon_regression_loss(prediction: Any, target: Any, valid_mask: Any) -> Any:
    """Continuous-density MSE only; invalid/no-data and unlabelled pixels do not enter loss."""
    _require_torch()
    expected = torch.as_tensor(target, dtype=prediction.dtype, device=prediction.device)
    mask = torch.as_tensor(valid_mask, dtype=torch.bool, device=prediction.device)
    if expected.shape != prediction.shape or mask.shape != prediction.shape:
        raise ValueError("Carbon target and valid mask must match the regression output shape.")
    mask &= torch.isfinite(expected)
    if not mask.any():
        raise ValueError("Regression patch has no valid continuous carbon targets.")
    return torch.square(prediction[mask] - expected[mask]).mean()


def train_encoder_decoder(
    model: Any,
    batches: Iterable[tuple[Any, Any, Any]],
    optimizer: Any,
    *,
    task: Literal["segmentation", "carbon_regression"],
    epochs: int,
    device: str = "cpu",
) -> list[float]:
    """Minimal train loop; callers must spatially split patches before creating batches."""
    _require_torch()
    if epochs < 1 or task not in {"segmentation", "carbon_regression"}:
        raise ValueError("Training needs positive epochs and a supported target task.")
    model.to(device)
    history = []
    for _epoch in range(epochs):
        model.train()
        losses = []
        for image, target, valid_mask in batches:
            image = torch.as_tensor(image, dtype=torch.float32, device=device)
            target = torch.as_tensor(target, device=device)
            valid_mask = torch.as_tensor(valid_mask, dtype=torch.bool, device=device)
            optimizer.zero_grad(set_to_none=True)
            prediction = model(image)
            if task == "segmentation":
                loss = masked_segmentation_loss(prediction, target, valid_mask)
            else:
                if prediction.ndim == 4 and prediction.shape[1] == 1:
                    prediction = prediction[:, 0]
                if target.ndim == 4 and target.shape[1] == 1:
                    target = target[:, 0]
                if valid_mask.ndim == 4 and valid_mask.shape[1] == 1:
                    valid_mask = valid_mask[:, 0]
                loss = masked_carbon_regression_loss(prediction, target, valid_mask)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        if not losses:
            raise ValueError("Training dataset produced no batches.")
        history.append(float(np.mean(losses)))
    return history


def model_contract(sensor_mode: str = "optical_only", optical_sensor: str = "sentinel2") -> dict[str, Any]:
    return {
        "architecture": "U-Net encoder-decoder",
        "release_status": "experimental_unvalidated",
        "inference_available": False,
        "weights_available": False,
        "class_schema": CLASS_SCHEMA,
        "class_schema_version": CLASS_SCHEMA_VERSION,
        "class_order": list(CLASS_ORDER),
        "class_map": CLASS_MAP,
        "label_source": None,
        "label_license": None,
        "sensor_mode": sensor_mode,
        "channel_order": list(channel_order(sensor_mode, optical_sensor)),
        "sensor_modes_evaluated": [],
        "validation": {"type": "not_validated", "metrics": None, "holdout": None},
        "availability_reason": "Bobot dan dataset label berlisensi belum dikonfigurasi; evaluasi spatial holdout belum tersedia.",
    }


def segmentation_model_catalog() -> dict[str, Any]:
    return {
        "models": [
            {
                "id": "savegeo_carbon_unet_v1",
                "name": "SaveGeo U-Net land-cover segmentation",
                **model_contract(mode),
            }
            for mode in SENSOR_MODES
        ],
        "class_schema": CLASS_SCHEMA,
        "class_schema_version": CLASS_SCHEMA_VERSION,
        "class_order": list(CLASS_ORDER),
        "class_map": CLASS_MAP,
        "separation_note": "Land-cover segmentation and continuous carbon-stock regression use separate heads, targets, masks, metrics, and model status.",
    }


def build_artifact_manifest(
    *, label_source: str, label_license: str, sensor_mode: str,
    training_blocks: Iterable[str], holdout_blocks: Iterable[str],
    validation: dict[str, Any] | None = None, optical_sensor: str = "sentinel2",
) -> dict[str, Any]:
    train = sorted({str(value) for value in training_blocks})
    holdout = sorted({str(value) for value in holdout_blocks})
    overlap = set(train) & set(holdout)
    if not label_source.strip() or not label_license.strip():
        raise ValueError("Training label source and license must be recorded.")
    if overlap:
        raise ValueError("Spatial block leakage between training and holdout blocks.")
    if sensor_mode not in SENSOR_MODES:
        raise ValueError("Unsupported sensor mode.")
    return {
        **model_contract(sensor_mode, optical_sensor),
        "label_source": label_source,
        "label_license": label_license,
        "training_blocks": train,
        "holdout_blocks": holdout,
        "validation": validation or {"type": "not_validated", "metrics": None, "holdout": "spatial_block"},
        "created_at": datetime.now(UTC).isoformat(),
    }


def save_model_artifact(model: Any, artifact_path: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Write weights outside the repository path chosen by the caller and checksum them."""
    _require_torch()
    target = Path(artifact_path).resolve()
    if manifest.get("release_status") == "production":
        raise ValueError("This experimental pipeline cannot mark a model production without the independent release workflow.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        torch.save(model.state_dict(), temporary_name)
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    metadata = {**manifest, "artifact_sha256": checksum, "artifact_version": "1"}
    metadata_path = target.with_suffix(target.suffix + ".json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    return {"artifact_path": str(target), "artifact_sha256": checksum, "metadata_path": str(metadata_path)}


def load_model_artifact(model: Any, artifact_path: str | Path, expected_sha256: str) -> dict[str, Any]:
    _require_torch()
    target = Path(artifact_path).resolve()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise ValueError("Encoder-decoder model artifact checksum mismatch.")
    state = torch.load(target, map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    metadata_path = target.with_suffix(target.suffix + ".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("artifact_sha256") != digest:
        raise ValueError("Encoder-decoder manifest checksum mismatch.")
    return metadata
