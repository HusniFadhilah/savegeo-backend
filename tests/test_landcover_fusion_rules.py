"""Rule table of the common-path fusion on synthetic constant images.

Needs a live Earth Engine session; it is skipped unless RUN_GEE_LIVE=1 so the offline suite stays offline.
"""
import itertools
import os
from datetime import date

import pytest

from app.services import landcover_fusion_service as fusion
from app.services.gee_common import AnalysisError

pytestmark = pytest.mark.skipif(os.environ.get("RUN_GEE_LIVE") != "1", reason="set RUN_GEE_LIVE=1 for live Earth Engine tests")

NAMES = ["p0", "p1", "p2"]
JOINT, OPT, SAR = [0.9, 0.05, 0.05], [0.05, 0.9, 0.05], [0.05, 0.05, 0.9]
LEVELS = [0.0, 0.3, 0.7]


@pytest.fixture(scope="module")
def ee_session():
    import ee
    from app.db.session import SessionLocal
    from app.services import gee_service

    db = SessionLocal()
    try:
        if not gee_service.initialize_ee(db):
            pytest.skip("Earth Engine credentials unavailable")
    finally:
        db.close()
    return ee


def _frame(ee, qo, qs):
    const = lambda v: ee.Image.constant(v).toFloat()
    vec = lambda v: ee.Image.constant(v).rename(NAMES).toFloat()
    return {"q_o": const(qo), "q_s": const(qs), "footprint": const(1).gt(0),
            "paths": {"joint": vec(JOINT), "optical": vec(OPT), "sar": vec(SAR)}}


def _run(ee, mode, q, qo0, qs0, qo1, qs1):
    aoi = ee.Geometry.Rectangle([104.70, -2.95, 104.701, -2.949])
    params = {"q_min": q, "fusion_mode": mode, "confidence_threshold": None}
    b = fusion.common_basis(_frame(ee, qo0, qs0), _frame(ee, qo1, qs1), [10, 20, 30], params, aoi)
    first = lambda image, band=None: image.reduceRegion(ee.Reducer.first(), aoi, 10)
    return ee.Dictionary({"path": first(b["path"]), "p0": first(b["p0"]), "p1": first(b["p1"])}).getInfo()


def _expected_path(q, qo0, qs0, qo1, qs1):
    optical = min(qo0, qo1) > 0 and min(qo0, qo1) >= q
    radar = min(qs0, qs1) > 0 and min(qs0, qs1) >= q
    return 3 if optical and radar else 1 if optical else 2 if radar else 0


def test_routing_rule_table_and_same_model_for_both_periods(ee_session):
    for qo0, qs0, qo1, qs1 in itertools.product(LEVELS, repeat=4):
        got = _run(ee_session, "routing", 0.5, qo0, qs0, qo1, qs1)
        expected = _expected_path(0.5, qo0, qs0, qo1, qs1)
        assert int(got["path"]["common_path"] or 0) == expected, (qo0, qs0, qo1, qs1)
        if expected:
            assert got["p0"] == got["p1"]
            assert [round(got["p0"][n], 3) for n in NAMES] == {1: OPT, 2: SAR, 3: JOINT}[expected]


def test_source_without_observation_never_passes_even_at_zero_threshold(ee_session):
    got = _run(ee_session, "routing", 0.0, 0, 0, 0, 0)
    assert int(got["path"]["common_path"] or 0) == 0


def test_weighted_mode_uses_minimum_weights_zeroed_below_threshold(ee_session):
    for qo0, qs0, qo1, qs1 in itertools.product(LEVELS, repeat=4):
        got = _run(ee_session, "weighted", 0.5, qo0, qs0, qo1, qs1)
        w_o = min(qo0, qo1) if min(qo0, qo1) >= 0.5 else 0.0
        w_s = min(qs0, qs1) if min(qs0, qs1) >= 0.5 else 0.0
        if w_o + w_s == 0:
            assert int(got["path"]["common_path"] or 0) == 0
            continue
        expected = [(w_o * OPT[k] + w_s * SAR[k]) / (w_o + w_s) for k in range(3)]
        assert [got["p0"][n] for n in NAMES] == pytest.approx(expected, abs=1e-3)
        assert got["p0"] == got["p1"]


def test_change_score_is_total_variation_distance(ee_session):
    ee = ee_session
    aoi = ee.Geometry.Rectangle([104.70, -2.95, 104.701, -2.949])
    vec = lambda v: ee.Image.constant(v).rename(NAMES).toFloat()
    score = fusion.change_score(vec([0.6, 0.4, 0.0]), vec([0.4, 0.6, 0.0]))
    assert score.reduceRegion(ee.Reducer.first(), aoi, 10).getInfo()["D"] == pytest.approx(0.2, abs=1e-4)


def test_flood_event_extent_uses_one_orbit_geometry(ee_session):
    ee = ee_session
    aoi = ee.Geometry.Rectangle([110.58, -6.93, 110.68, -6.85])  # Demak, Feb-Mar 2024 floods
    event = fusion.event_mask({"type": "flood_sar", "pre_date": date(2024, 1, 20), "post_date": date(2024, 3, 18)}, aoi)
    area = ee.Image.pixelArea().divide(10000).updateMask(event).reduceRegion(ee.Reducer.sum(), aoi, 30, maxPixels=1e9).get("area").getInfo()
    assert area and area > 100  # new inundation inside a rice-growing floodplain


def test_flood_event_extent_without_shared_orbit_is_an_error(ee_session):
    ee = ee_session
    aoi = ee.Geometry.Rectangle([110.58, -6.93, 110.68, -6.85])
    with pytest.raises(AnalysisError) as error:
        # pre window far before Sentinel-1B/C coverage of this AOI: no shared orbit between the windows
        fusion.sar_water_pair(aoi, date(2015, 1, 20), date(2024, 3, 18))
    assert error.value.status_code == 404
