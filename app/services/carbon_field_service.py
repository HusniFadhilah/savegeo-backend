"""Field normalization and plot-independent carbon calibration (no GEE dependency)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math

import numpy as np
from pydantic import ValidationError

from app.schemas.carbon_field import FieldAssessmentRequest, FieldObservationInput
from app.services.carbon_calibration_service import evaluate_predictions, lopo_linear_calibration

POOL_TARGETS = {
    "agb": "aboveground_biomass_carbon",
    "bgb": "belowground_biomass_carbon",
    "soc": "soil_organic_carbon",
    "litter": "litter_carbon",
    "deadwood": "deadwood_carbon",
}
SOC_SOURCE = "https://www.fao.org/fileadmin/user_upload/GSP/MRV/GSP_MRV_Protocol_A4_EN_002.pdf"
CSV_COLUMNS = [
    "plot_id",
    "survey_date",
    "pool",
    "role",
    "latitude",
    "longitude",
    "plot_area_m2",
    "reference_type",
    "method",
    "source",
    "quantity",
    "unit",
    "value",
    "carbon_fraction",
    "depth_top_cm",
    "depth_bottom_cm",
    "soil_layers_json",
    "litter_sample_json",
    "deadwood_sample_json",
    "prediction_source",
    "prediction_year",
    "prediction_mg_c_ha",
    "prediction_scale_m",
    "prediction_support",
    "prediction_depth_top_cm",
    "prediction_depth_bottom_cm",
]


def normalize(record: FieldObservationInput) -> dict:
    if record.litter_sample:
        sample = record.litter_sample
        dry_kg = sample.wet_mass_kg * (sample.subsample_dry_kg / sample.subsample_wet_kg)
        observed = dry_kg * 10 / (sample.quadrat_count * sample.quadrat_area_m2) * record.carbon_fraction
    elif record.deadwood_sample:
        dry_kg = sum(piece.volume_m3 * piece.dry_density_kg_m3 for piece in record.deadwood_sample.pieces)
        observed = dry_kg * 10 / record.plot_area_m2 * record.carbon_fraction
    elif record.soil_layers:
        # C [g/kg] * fine-earth BD [g/cm3] * thickness [cm] * 0.1 -> Mg C/ha.
        observed = sum(
            layer.organic_carbon_g_kg
            * layer.bulk_density_g_cm3
            * (layer.bottom_cm - layer.top_cm)
            * 0.1
            * (1 - layer.coarse_fragments_volume_pct / 100)
            for layer in record.soil_layers
        )
    else:
        observed = record.value
        if record.unit == "kg/plot":
            observed = observed * 10 / record.plot_area_m2
        if record.quantity == "dry_biomass":
            observed *= record.carbon_fraction
    if not math.isfinite(observed):
        raise ValueError("Hasil normalisasi tidak berhingga; periksa angka/satuan input.")
    raw = record.model_dump(mode="json", exclude={"prediction"})
    # Keep pre-existing measurement checksums stable when optional sample modes are absent.
    for key in ("litter_sample", "deadwood_sample"):
        if raw[key] is None:
            raw.pop(key)
    identity = [
        record.plot_id,
        round(record.latitude, 5),
        round(record.longitude, 5),
        str(record.survey_date),
        record.pool,
        record.depth_top_cm,
        record.depth_bottom_cm,
    ]
    return {
        "identity_key": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
        "sha256": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(),
        "raw": raw,
        "observed_mg_c_ha": observed,
        "prediction": record.prediction.model_dump(mode="json") if record.prediction else None,
        "warnings": (
            ["BGB merupakan referensi turunan, bukan pengukuran akar independen."]
            if record.pool == "bgb" and record.reference_type in {"allometry", "root_shoot_ratio"}
            else []
        ),
    }


def read_csv(data: bytes) -> dict:
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Simpan CSV dalam encoding UTF-8.") from exc
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("Header CSV kosong atau memiliki kolom duplikat.")
    unknown = set(reader.fieldnames) - set(CSV_COLUMNS)
    if unknown:
        raise ValueError(f"Kolom CSV tidak dikenal: {', '.join(sorted(unknown))}")
    records, preview, errors = [], [], []
    identities = set()
    for index, row in enumerate(reader, start=2):
        if index > 2001:
            raise ValueError("Maksimum 2000 baris per impor.")
        try:
            if None in row:
                raise ValueError("Jumlah kolom tidak sesuai header CSV.")
            payload = {
                key: value.strip()
                for key, value in row.items()
                if value
                and value.strip()
                and key not in {"soil_layers_json", "litter_sample_json", "deadwood_sample_json"}
                and not key.startswith("prediction_")
            }
            if (row.get("soil_layers_json") or "").strip():
                payload["soil_layers"] = json.loads(row["soil_layers_json"])
            for key in ("litter_sample", "deadwood_sample"):
                if (row.get(key + "_json") or "").strip():
                    payload[key] = json.loads(row[key + "_json"])
            if any(value and value.strip() for key, value in row.items() if key.startswith("prediction_")):
                names = {
                    "source": "source",
                    "year": "year",
                    "mg_c_ha": "value_mg_c_ha",
                    "scale_m": "scale_m",
                    "support": "support",
                    "depth_top_cm": "depth_top_cm",
                    "depth_bottom_cm": "depth_bottom_cm",
                }
                payload["prediction"] = {
                    target: row[f"prediction_{key}"].strip()
                    for key, target in names.items()
                    if (row.get(f"prediction_{key}") or "").strip()
                }
            record = FieldObservationInput.model_validate(payload)
            normalized = normalize(record)
            if normalized["identity_key"] in identities:
                raise ValueError("Plot/tanggal/pool/kedalaman duplikat dalam berkas.")
            identities.add(normalized["identity_key"])
            records.append(record)
            preview.append(
                {
                    "row": index,
                    "plot_id": record.plot_id,
                    "pool": record.pool,
                    "role": record.role,
                    "observed_mg_c_ha": normalized["observed_mg_c_ha"],
                    "warnings": normalized["warnings"],
                }
            )
        except ValidationError as exc:
            errors.append(
                {
                    "row": index,
                    "message": "; ".join(
                        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                        for item in exc.errors()
                    )[:1500],
                }
            )
        except (ValueError, TypeError) as exc:
            errors.append({"row": index, "message": str(exc)[:1500]})
    if not preview and not errors:
        errors.append({"row": 1, "message": "CSV tidak memiliki baris pengukuran."})
    return {"records": records, "preview": preview, "errors": errors, "valid": not errors}


def field_inventory(observations: list[dict]) -> dict:
    pools = {}
    for pool in POOL_TARGETS:
        rows = [row for row in observations if row["pool"] == pool]
        pools[pool] = {
            "records": len(rows),
            "calibration_plots": len(_group([_plot_reference(row) for row in rows if row["role"] == "calibration"])),
            "validation_plots": len(_group([_plot_reference(row) for row in rows if row["role"] == "validation"])),
            "without_prediction": sum(not row["predictions"] for row in rows),
            "sources": sorted({prediction["source"] for row in rows for prediction in row["predictions"]}),
        }
    return {"pools": pools, "total_records": len(observations), "unit": "Mg C/ha"}


def _group(rows):
    groups: list[list[dict]] = []
    for row in rows:
        group = next((group for group in groups if _same_physical_plot(group[0], row)), None)
        if group is None:
            groups.append([row])
        else:
            group.append(row)
    return [
        {
            "plot_id": _plot_label(values[0]),
            "observed": float(np.mean([r["observed"] for r in values])),
            "predicted": float(np.mean([r["predicted"] for r in values])),
        }
        for values in groups
    ]


_PLOT_MATCH_RADIUS_M = 30.0


def _distance_m(left: dict, right: dict) -> float:
    lat1, lat2 = math.radians(left["latitude"]), math.radians(right["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(right["longitude"] - left["longitude"])
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _same_physical_plot(left: dict, right: dict) -> bool:
    return left["plot_id"] == right["plot_id"] and _distance_m(left, right) <= _PLOT_MATCH_RADIUS_M


def _plot_label(row: dict) -> str:
    return f"{row['plot_id']}@{row['latitude']:.5f},{row['longitude']:.5f}"


def _plot_reference(observation: dict) -> dict:
    raw = observation["raw"]
    return {
        "plot_id": raw["plot_id"],
        "latitude": raw["latitude"],
        "longitude": raw["longitude"],
        "observed": observation.get("observed_mg_c_ha", 0),
        "predicted": 0,
    }


def _evaluate_validation(rows: list[dict]) -> dict:
    """Report a matched one-plot holdout without implying population accuracy."""
    if len(rows) != 1:
        return evaluate_predictions(rows)
    row = rows[0]
    residual = float(row["predicted"] - row["observed"])
    return {
        "status": "computed",
        "n_plots": 1,
        "bias": residual,
        "rmse": abs(residual),
        "mae": abs(residual),
        "r2": None,
        "qualification": "single_plot_holdout_not_generalizable",
        "rows": [{**row, "residual": residual, "absolute_error": abs(residual)}],
    }


def assess(observations: list[dict], request: FieldAssessmentRequest) -> dict:
    paired = {"calibration": [], "validation": []}
    exclusions = []
    reference_types = set()
    for observation in observations:
        if observation["pool"] != request.pool:
            continue
        raw = observation["raw"]
        predictions = [p for p in observation["predictions"] if p["source"] == request.prediction_source]
        reason = None
        if len(predictions) != 1:
            reason = "prediction_missing_or_ambiguous"
        else:
            prediction = predictions[0]
            if abs(prediction["year"] - int(raw["survey_date"][:4])) > request.max_year_gap:
                reason = "year_mismatch"
            elif prediction["support"] != "plot_mean" and not request.allow_point_pixel:
                reason = "point_pixel_support_not_accepted"
            elif request.pool == "soc" and any(
                raw.get(key) != getattr(request, key) or prediction.get(key) != getattr(request, key)
                for key in ("depth_top_cm", "depth_bottom_cm")
            ):
                reason = "soil_depth_mismatch"
        if reason:
            exclusions.append({"id": observation["id"], "plot_id": raw["plot_id"], "reason": reason})
        else:
            reference_types.add(raw["reference_type"])
            paired[observation["role"]].append(
                {
                    "plot_id": raw["plot_id"],
                    "latitude": raw["latitude"],
                    "longitude": raw["longitude"],
                    "observed": observation["observed_mg_c_ha"],
                    "predicted": prediction["value_mg_c_ha"],
                }
            )
    train, validation = _group(paired["calibration"]), _group(paired["validation"])
    if any(_same_physical_plot(calibration, held_out) for calibration in paired["calibration"] for held_out in paired["validation"]):
        raise ValueError(
            "Plot yang sama tidak boleh berada pada kalibrasi dan validasi, termasuk antar kunjungan."
        )
    result = {
        "schema_version": "carbon-field-assessment/v1",
        "configuration": request.model_dump(),
        "unit": "Mg C/ha",
        "independent_sample_unit": "plot",
        "matched_records": sum(len(r) for r in paired.values()),
        "calibration_plots": len(train),
        "sample_count": len(train),
        "validation_method": "leave_one_plot_out_and_separate_validation",
        "validation_plots": len(validation),
        "excluded": exclusions,
        "reference_types": sorted(reference_types),
        "baseline_calibration": evaluate_predictions(train),
        "baseline_validation": _evaluate_validation(validation),
        "cross_validation": lopo_linear_calibration(train, clip_min=0.0),
        "method_note": "Kunjungan berulang dirata-ratakan per plot sebelum fit dan validasi; plot diberi bobot sama. Validasi terpisah tidak dipakai untuk fit. Hasil tetap lokal dan eksperimental.",
        "warnings": [
            "Sampel purposif tidak mendukung klaim akurasi nasional; periksa keterwakilan habitat dan cakupan spasial."
        ],
    }
    if request.pool == "bgb" and reference_types & {"allometry", "root_shoot_ratio"}:
        result["warnings"].append(
            "BGB mencakup allometri/rasio akar:tajuk; ini evaluasi terhadap referensi turunan, bukan validasi akar independen."
        )
    if request.allow_point_pixel:
        result["warnings"].append(
            "Piksel titik diizinkan eksplisit; dukungan spasialnya berbeda dari rerata plot."
        )
    if request.create_calibration_candidate and len(train) >= 3 and np.ptp([r["predicted"] for r in train]) > 0:
        slope, intercept = np.polyfit([r["predicted"] for r in train], [r["observed"] for r in train], 1)
        result["artifact"] = {
            "schema_version": "carbon-calibration-model/v1",
            "target_pool": POOL_TARGETS[request.pool],
            "target_unit": "Mg C/ha",
            "method": "linear_calibration",
            "slope": float(slope),
            "intercept": float(intercept),
            "prediction_source": request.prediction_source,
            "clip_min": 0.0,
            "depth_cm": [request.depth_top_cm, request.depth_bottom_cm] if request.pool == "soc" else None,
        }
        result["calibrated_validation"] = _evaluate_validation(
            [{**r, "predicted": max(0.0, float(intercept + slope * r["predicted"]))} for r in validation]
        )
    else:
        result["artifact"] = None
        if request.create_calibration_candidate:
            result["warnings"].append(
                "Kalibrasi linear memerlukan minimal tiga plot kalibrasi dan variasi nilai prediktor."
            )
        else:
            result["warnings"].append(
                "Run otomatis ini hanya untuk validasi; kandidat kalibrasi tidak dibuat. Jalankan evaluasi manual setelah cakupan batch lengkap bila memerlukan kandidat."
            )
    result["status"] = "computed" if train or validation else "not_computed"
    return result
