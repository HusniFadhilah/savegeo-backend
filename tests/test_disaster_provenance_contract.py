import datetime as dt
from types import SimpleNamespace

from app.services.disaster_capability_service import build_provenance, validate_persisted_result


def _event():
    return SimpleNamespace(id=1, disaster_type="flood")


def _aoi():
    return SimpleNamespace(id=2, event_id=1, geojson={"type": "Polygon", "coordinates": []})


def _image(phase, image_id):
    return SimpleNamespace(
        id=image_id, event_id=1, phase=phase, satellite="Sentinel-1", sensor="VV",
        source_kind="gee", acquisition_date=dt.date(2026, 1, 1 if phase == "pre" else 2),
        resolution_m=10, cloud_coverage_pct=None, scene_id=f"scene-{image_id}",
    )


def test_provenance_contains_audit_fields_for_every_result():
    from app.registries.disaster_model_registry import get_model

    provenance = build_provenance(_event(), _aoi(), _image("pre", 10), _image("post", 11), get_model("flood_change_v2"))
    assert provenance["event_id"] == 1
    assert provenance["aoi_id"] == 2
    assert provenance["pre_imagery_id"] == 10
    assert provenance["post_imagery_id"] == 11
    assert provenance["source_datasets"]
    assert provenance["input_fingerprint"]
    assert "generated_at" in provenance
    assert "quality_flags" in provenance


def test_incomplete_provenance_is_rejected_as_stale():
    result = SimpleNamespace(
        tile_url="https://tiles.example/{z}/{x}/{y}", statistics={"coverage_pct": 100},
        features=None, is_published=False, provenance={"event_id": 1}, coverage={"coverage_pct": 100},
    )
    run = SimpleNamespace(model_id="flood_change_v2", status="completed", aoi_id=2, pre_imagery_id=10, post_imagery_id=11, parameters={})
    check = validate_persisted_result(None, _event(), run, result, require_published=False)
    assert check.allowed is False
    assert check.status == "stale"
    assert "Provenance tidak lengkap" in check.reasons[0]
