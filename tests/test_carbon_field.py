from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.carbon_field import FieldAssessmentRequest, FieldObservationInput
from app.services.carbon_field_service import CSV_COLUMNS, assess, normalize, read_csv
from app.services.carbon_calibration_service import lopo_linear_calibration


def record(**changes):
    data = dict(
        plot_id="P1",
        survey_date="2025-06-01",
        pool="agb",
        role="calibration",
        latitude=-7,
        longitude=110,
        plot_area_m2=400,
        reference_type="field_measurement",
        method="Harvest dry mass",
        source="Field report 2025",
        quantity="carbon",
        unit="Mg/ha",
        value=10,
    )
    return FieldObservationInput.model_validate({**data, **changes})


def observation(plot, predicted, observed, role="calibration", **changes):
    normalized = normalize(record(plot_id=plot, value=observed, role=role, **changes))
    return {
        "id": f"{plot}-{role}",
        "raw": normalized["raw"],
        "pool": changes.get("pool", "agb"),
        "role": role,
        "observed_mg_c_ha": observed,
        "predictions": [
            dict(source="model-v1", year=2025, value_mg_c_ha=predicted, support="plot_mean", scale_m=30)
        ],
    }


@pytest.mark.parametrize("pool", ["agb", "bgb", "litter", "deadwood"])
def test_mass_area_and_carbon_conversion(pool):
    value = normalize(
        record(pool=pool, value=1000, unit="kg/plot", quantity="dry_biomass", carbon_fraction=0.4)
    )
    assert value["observed_mg_c_ha"] == pytest.approx(10)


@pytest.mark.parametrize(
    "changes",
    [
        dict(value=float("nan")),
        dict(plot_area_m2=0),
        dict(latitude=91),
        dict(quantity="dry_biomass"),
        dict(pool="agb", reference_type="root_shoot_ratio"),
        dict(survey_date="2100-01-01"),
    ],
)
def test_invalid_units_geometry_dates_and_missing_fractions_rejected(changes):
    with pytest.raises(ValidationError):
        record(**changes)


def soil(**changes):
    return record(
        pool="soc",
        reference_type="laboratory",
        value=None,
        depth_top_cm=0,
        depth_bottom_cm=30,
        soil_layers=[
            dict(
                top_cm=0,
                bottom_cm=30,
                organic_carbon_g_kg=20,
                bulk_density_g_cm3=1.2,
                coarse_fragments_volume_pct=10,
                bulk_density_basis="fine_earth_per_fine_earth_volume",
            )
        ],
        **changes,
    )


def test_soil_layer_stock_uses_volume_fraction_and_correct_units():
    assert normalize(soil())["observed_mg_c_ha"] == pytest.approx(64.8)


@pytest.mark.parametrize("layers", [[(0, 10), (15, 30)], [(0, 20), (15, 30)], [(5, 30)]])
def test_soil_layers_cannot_overlap_or_have_gaps(layers):
    data = soil().model_dump()
    data["soil_layers"] = [
        {**data["soil_layers"][0], "top_cm": top, "bottom_cm": bottom} for top, bottom in layers
    ]
    with pytest.raises(ValidationError):
        FieldObservationInput.model_validate(data)


def test_fit_excludes_independent_validation_and_aggregates_repeat_visits():
    rows = [observation(f"P{i}", i, 2 * i + 3) for i in range(1, 5)]
    rows += [observation("V1", 5, 99, "validation"), observation("V2", 6, 100, "validation")]
    repeat = observation("P1", 1, 5)
    repeat["id"] = "P1-repeat"
    repeat["raw"]["survey_date"] = "2025-12-01"
    rows.append(repeat)
    result = assess(rows, FieldAssessmentRequest(pool="agb", prediction_source="model-v1"))
    assert result["calibration_plots"] == 4
    assert result["artifact"]["slope"] == pytest.approx(2)
    assert result["artifact"]["intercept"] == pytest.approx(3)
    assert result["cross_validation"]["rmse"] < 1e-10
    assert result["calibrated_validation"]["rmse"] > 50


def test_same_plot_cannot_leak_across_calibration_and_validation():
    with pytest.raises(ValueError, match="Plot yang sama"):
        assess(
            [observation("P1", 1, 3), observation("P1", 2, 5, "validation")],
            FieldAssessmentRequest(pool="agb", prediction_source="model-v1"),
        )


def test_year_pixel_support_and_soil_depth_mismatches_are_visible():
    rows = [observation("P1", 1, 3), observation("P2", 2, 5)]
    rows[0]["predictions"][0]["year"] = 2020
    rows[1]["predictions"][0]["support"] = "point_pixel"
    result = assess(rows, FieldAssessmentRequest(pool="agb", prediction_source="model-v1"))
    assert {r["reason"] for r in result["excluded"]} == {"year_mismatch", "point_pixel_support_not_accepted"}
    row = observation("S1", 1, 3, pool="soc", reference_type="laboratory", depth_top_cm=0, depth_bottom_cm=30)
    row["predictions"][0].update(depth_top_cm=0, depth_bottom_cm=100)
    assert (
        assess(
            [row],
            FieldAssessmentRequest(
                pool="soc", prediction_source="model-v1", depth_top_cm=0, depth_bottom_cm=30
            ),
        )["excluded"][0]["reason"]
        == "soil_depth_mismatch"
    )


def test_bgb_derived_reference_has_explicit_warning():
    rows = [observation("P1", 1, 3, pool="bgb", reference_type="root_shoot_ratio")]
    result = assess(rows, FieldAssessmentRequest(pool="bgb", prediction_source="model-v1"))
    assert any("referensi turunan" in warning for warning in result["warnings"])


def test_lopo_final_model_refits_all_plots_not_mean_fold_coefficients():
    import numpy as np

    rows = [
        dict(plot_id=str(i), predicted=x, observed=y)
        for i, (x, y) in enumerate([(1, 2), (2, 6), (3, 4), (10, 20)])
    ]
    expected = np.polyfit([r["predicted"] for r in rows], [r["observed"] for r in rows], 1)
    result = lopo_linear_calibration(rows)
    assert result["calibration_slope"] == pytest.approx(expected[0])
    assert result["calibration_intercept"] == pytest.approx(expected[1])


def test_csv_keeps_valid_rows_for_preview_but_blocks_any_invalid_row():
    import csv
    import io

    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    row = record().model_dump(
        mode="json", exclude={"prediction", "soil_layers", "litter_sample", "deadwood_sample"}
    )
    writer.writerow(row)
    writer.writerow({**row, "plot_id": "bad", "value": "nan"})
    result = read_csv(stream.getvalue().encode())
    assert result["valid"] is False and len(result["records"]) == 1
    assert result["errors"][0]["row"] == 3


def test_import_is_idempotent_and_role_leakage_cannot_modify_stored_data():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.db.models.carbon_calibration import CarbonCalibrationDataset, CarbonFieldObservation
    from app.db.models.admin_user import AdminUser
    from app.db.models.audit_log import AuditLog
    from app.api.routes.carbon_field import import_records
    from fastapi import HTTPException

    @compiles(JSONB, "sqlite")
    def json_type(*args, **kwargs):
        return "JSON"

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            AdminUser.__table__,
            CarbonCalibrationDataset.__table__,
            CarbonFieldObservation.__table__,
            AuditLog.__table__,
        ],
    )
    with Session(engine) as db:
        db.add(AdminUser(id=1, username="test", password_hash="test"))
        dataset = CarbonCalibrationDataset(dataset_id="field-test", name="Field test", manifest={})
        db.add(dataset)
        db.commit()
        admin = SimpleNamespace(id=1)
        assert import_records(db, dataset, [record()], admin)["created"] == 1
        assert import_records(db, dataset, [record()], admin)["replayed"] == 1
        assert db.query(CarbonFieldObservation).count() == 1 and db.query(AuditLog).count() == 2
        with pytest.raises(HTTPException):
            import_records(
                db,
                dataset,
                [record(plot_id="P2"), record(role="validation", survey_date="2024-06-01")],
                admin,
            )
        db.rollback()
        assert db.query(CarbonFieldObservation).count() == 1
    engine.dispose()


def test_litter_composite_uses_sampled_quadrat_area_not_parent_plot_area():
    item = record(
        pool="litter",
        value=None,
        quantity="dry_biomass",
        unit="kg/plot",
        carbon_fraction=0.37,
        litter_sample=dict(
            wet_mass_kg=4,
            subsample_wet_kg=0.2,
            subsample_dry_kg=0.1,
            quadrat_count=4,
            quadrat_area_m2=0.25,
            maximum_diameter_cm=10,
        ),
    )
    result = normalize(item)
    assert result["observed_mg_c_ha"] == pytest.approx(7.4)
    assert result["raw"]["litter_sample"]["quadrat_count"] == 4


@pytest.mark.parametrize(
    "changes", [dict(subsample_dry_kg=0.3), dict(subsample_wet_kg=5), dict(quadrat_area_m2=200)]
)
def test_invalid_litter_samples_are_rejected(changes):
    sample = dict(
        wet_mass_kg=4,
        subsample_wet_kg=0.2,
        subsample_dry_kg=0.1,
        quadrat_count=4,
        quadrat_area_m2=0.25,
        maximum_diameter_cm=10,
    )
    with pytest.raises(ValidationError):
        record(
            pool="litter",
            value=None,
            quantity="dry_biomass",
            unit="kg/plot",
            carbon_fraction=0.37,
            litter_sample={**sample, **changes},
        )


def test_deadwood_volume_retains_position_decay_and_density_source():
    result = normalize(
        record(
            pool="deadwood",
            value=None,
            quantity="dry_biomass",
            unit="kg/plot",
            carbon_fraction=0.47,
            deadwood_sample={
                "minimum_diameter_cm": 10,
                "pieces": [
                    dict(
                        component="standing",
                        volume_m3=1,
                        dry_density_kg_m3=300,
                        decay_class="II",
                        density_source="Field sample",
                    ),
                    dict(
                        component="downed",
                        volume_m3=0.5,
                        dry_density_kg_m3=200,
                        decay_class="III",
                        density_source="Lab result",
                    ),
                ],
            },
        )
    )
    assert result["observed_mg_c_ha"] == pytest.approx(4.7)
    assert result["raw"]["deadwood_sample"]["pieces"][1]["decay_class"] == "III"


def test_laboratory_bgb_is_not_mislabeled_as_a_root_shoot_estimate():
    result = normalize(record(pool="bgb", reference_type="laboratory"))
    assert not result["warnings"]
    report = assess(
        [observation("P1", 10, 12, pool="bgb", reference_type="laboratory")],
        FieldAssessmentRequest(pool="bgb", prediction_source="model-v1"),
    )
    assert not any("rasio akar" in warning for warning in report["warnings"])


def test_nonnegative_transform_matches_evaluation_and_saved_artifact():
    rows = [observation(f"P{i}", x, y) for i, (x, y) in enumerate([(1, 0), (2, 1), (3, 3), (4, 5)])]
    rows += [observation("V1", 0, 0, "validation"), observation("V2", 1, 0, "validation")]
    report = assess(rows, FieldAssessmentRequest(pool="agb", prediction_source="model-v1"))
    assert report["artifact"]["clip_min"] == 0
    assert all(r["predicted"] >= 0 for r in report["cross_validation"]["rows"])
    assert all(r["predicted"] >= 0 for r in report["calibrated_validation"]["rows"])


def test_optional_new_sample_modes_preserve_old_record_checksums():
    import hashlib
    import json

    item = record()
    legacy = item.model_dump(mode="json", exclude={"prediction", "litter_sample", "deadwood_sample"})
    assert (
        normalize(item)["sha256"] == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    )


def test_inventory_counts_distinct_plots_and_collects_sources_across_visits():
    from app.services.carbon_field_service import field_inventory

    rows = [
        observation("P1", 1, 2),
        observation("P1", 2, 3),
        observation("V1", 2, 3, "validation"),
        observation("B1", 3, 4, pool="bgb"),
    ]
    rows[-1]["predictions"] = []
    result = field_inventory(rows)
    assert result["pools"]["agb"]["records"] == 3
    assert result["pools"]["agb"]["calibration_plots"] == 1
    assert result["pools"]["agb"]["validation_plots"] == 1
    assert result["pools"]["agb"]["sources"] == ["model-v1"]
    assert result["pools"]["bgb"]["without_prediction"] == 1
    assert set(result["pools"]) == {"agb", "bgb", "soc", "litter", "deadwood"}
