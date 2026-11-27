"""Auditable screening ledger and buffered spatial calibration.

No calculation here determines eligibility or issues credits. User-supplied
methodology rules remain visible in the input snapshot for independent review.
"""

from __future__ import annotations

from typing import Any

from decimal import Decimal, localcontext

import numpy as np
from sklearn.model_selection import GroupKFold

from app.schemas.dmrv_integrity import AccountingInput, ValidationInput


def stock_change(data: AccountingInput, area_ha: float) -> dict:
    with localcontext() as context:
        context.prec = 34
        def dec(value):
            return Decimal(str(value))
        area = dec(area_ha)
        lines = []
        gross = Decimal(0)
        for pool in data.pools:
            project = (dec(pool.project_end) - dec(pool.project_start)) * area
            baseline = (dec(pool.baseline_end) - dec(pool.baseline_start)) * area
            additional = project - baseline
            co2 = additional * Decimal(44) / Decimal(12)
            gross += co2
            lines.append(
                {
                    "pool": pool.pool,
                    "project_change_mg_c": float(project),
                    "baseline_change_mg_c": float(baseline),
                    "additional_change_mg_c": float(additional),
                    "additional_tco2": float(co2),
                }
            )
        required = ("project_emissions_tco2e", "leakage_tco2e", "uncertainty_deduction_pct", "buffer_pct")
        missing = [key for key in required if getattr(data, key) is None]
        net = (
            None
            if any(getattr(data, k) is None for k in required[:2])
            else (gross - dec(data.project_emissions_tco2e) - dec(data.leakage_tco2e))
        )
        after_uncertainty = (
            None
            if net is None or data.uncertainty_deduction_pct is None
            else (net if net <= 0 else net * (1 - dec(data.uncertainty_deduction_pct) / 100))
        )
        buffer = (
            None
            if after_uncertainty is None or data.buffer_pct is None
            else (max(Decimal(0), after_uncertainty) * dec(data.buffer_pct) / 100)
        )
        scenario = None if buffer is None else max(Decimal(0), after_uncertainty - buffer)
        return {
            "algorithm": "stock_change_screening_v1",
            "input_unit": "Mg C/ha",
            "area_ha": area_ha,
            "lines": lines,
            "gross_additional_tco2": float(gross),
            "net_mitigation_tco2e": float(net) if net is not None else None,
            "after_uncertainty_tco2e": float(after_uncertainty) if after_uncertainty is not None else None,
            "buffer_reserve_tco2e": float(buffer) if buffer is not None else None,
            "scenario_units_tco2e": float(scenario) if scenario is not None else None,
            "reversal_tco2e": float(max(Decimal(0), -net)) if net is not None else None,
            "missing_inputs": missing,
            "eligibility": "not_determined",
            "issuance_status": "not_issued",
            "deduction_policy": "Positive net × (1 − uncertainty %) then × buffer %; losses retained separately.",
            "qualification": "User-configured stock-change screening; methodology compliance requires independent assessment.",
        }


def metrics(observed: np.ndarray, predicted: np.ndarray) -> dict:
    residual = predicted - observed
    sst = float(np.square(observed - observed.mean()).sum())
    return {
        "n": len(observed),
        "r2": 1 - float(np.square(residual).sum()) / sst if sst > 0 else None,
        "rmse": float(np.sqrt(np.square(residual).mean())),
        "mae": float(np.abs(residual).mean()),
        "bias": float(residual.mean()),
    }


def _near_test(
    lon: np.ndarray, lat: np.ndarray, train: np.ndarray, test: np.ndarray, km: float
) -> np.ndarray:
    """Haversine distance, chunked to keep peak memory bounded."""
    keep = np.ones(len(train), dtype=bool)
    for start in range(0, len(train), 128):
        ids = train[start : start + 128]
        p1, p2 = np.radians(lat[ids])[:, None], np.radians(lat[test])[None, :]
        delta = np.radians(lon[test])[None, :] - np.radians(lon[ids])[:, None]
        a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(delta / 2) ** 2
        dist = 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        keep[start : start + len(ids)] = (dist >= km).all(axis=1)
    return train[keep]


def _bootstrap(y: np.ndarray, pred: np.ndarray, groups: np.ndarray) -> dict:
    rng = np.random.default_rng(20261113)
    blocks = np.unique(groups)
    indices = [np.flatnonzero(groups == block) for block in blocks]
    values: dict[str, list[float]] = {key: [] for key in ("r2", "rmse", "mae", "bias")}
    if len(blocks) < 3:
        return {"status": "insufficient_blocks", "replicates": 0, "ci95": {}}
    for _ in range(300):
        ids = np.concatenate([indices[i] for i in rng.integers(0, len(blocks), len(blocks))])
        for key, value in metrics(y[ids], pred[ids]).items():
            if key in values and value is not None:
                values[key].append(value)
    return {
        "status": "conditional_fixed_predictions",
        "replicates": 300,
        "ci95": {
            key: [float(np.quantile(vals, 0.025)), float(np.quantile(vals, 0.975))] if vals else None
            for key, vals in values.items()
        },
    }


def spatial_validation(data: ValidationInput) -> dict:
    pairs = data.pairs
    y = np.array([p.observed for p in pairs], dtype=float)
    pred = np.array([p.predicted for p in pairs], dtype=float)
    lon, lat = np.array([p.lon for p in pairs]), np.array([p.lat for p in pairs])
    block_names = [
        f"{int(np.floor(p.lon / data.block_degrees))}:{int(np.floor(p.lat / data.block_degrees))}"
        for p in pairs
    ]
    _, groups = np.unique(block_names, return_inverse=True)
    nblocks = len(np.unique(groups))
    oof = np.full(len(y), np.nan)
    folds, diagnostics = np.full(len(y), -1, dtype=int), []
    if nblocks >= 3 and len(y) >= 15:
        for fold, (train, test) in enumerate(GroupKFold(3).split(pred, y, groups)):
            kept = _near_test(lon, lat, train, test, data.buffer_km)
            entry: dict[str, Any] = {
                "fold": fold,
                "test_n": len(test),
                "train_before_buffer": len(train),
                "train_n": len(kept),
            }
            if len(kept) < 5 or np.ptp(pred[kept]) < 1e-12:
                entry["status"] = "insufficient_training_data"
            else:
                design = np.column_stack([np.ones(len(kept)), pred[kept]])
                intercept, slope = np.linalg.lstsq(design, y[kept], rcond=None)[0]
                oof[test] = np.maximum(0, intercept + slope * pred[test])
                folds[test] = fold
                entry.update(status="evaluated", intercept=float(intercept), slope=float(slope))
            diagnostics.append(entry)
    valid = np.isfinite(oof)
    complete = bool(valid.all())
    warnings = []
    if data.reference_year != data.predictor_year:
        warnings.append("cross_year_reference_predictor")
    if nblocks < 10:
        warnings.append("few_spatial_blocks")
    if data.label_source == "reference_map":
        warnings.append("map_agreement_not_field_accuracy")
    if not complete:
        warnings.append("spatial_calibration_incomplete")
    return {
        "algorithm": "buffered_spatial_linear_calibration_v1",
        "unit": data.unit,
        "n_blocks": nblocks,
        "raw_map": metrics(y, pred),
        "raw_map_bootstrap": _bootstrap(y, pred, groups),
        "calibration_status": "completed" if complete else "insufficient_spatial_support",
        "calibrated_oof": metrics(y, oof) if complete else None,
        "calibrated_bootstrap": _bootstrap(y, oof, groups) if complete else None,
        "folds": diagnostics,
        "warnings": warnings,
        "qualification": "Calibration holdout applies to fitted linear calibration only; upstream map/model independence is not established.",
        "oof": [
            {
                "point_id": p.point_id,
                "block": block_names[i],
                "fold": int(folds[i]),
                "observed": p.observed,
                "raw_prediction": p.predicted,
                "calibrated_prediction": float(oof[i]) if valid[i] else None,
            }
            for i, p in enumerate(pairs)
        ],
    }
