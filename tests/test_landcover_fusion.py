from datetime import date, timedelta

import pytest

from app.api.routes import analysis_jobs
from app.core.viewer_features import FEATURE_CODES
from app.services import landcover_fusion_service as fusion
from app.services.gee_common import AnalysisError

AOI = {"geojson": {"type": "Polygon", "coordinates": [[[104.7, -2.95], [104.73, -2.95], [104.73, -2.92], [104.7, -2.95]]]}}


def test_parse_params_defaults_and_years():
    params = fusion.parse_params({"aoi": AOI, "from_year": 2020, "to_year": 2024})
    assert params["t0"] == ("2020-01-01", "2021-01-01")
    assert params["t1"][0] == "2024-01-01"
    assert params["fusion_mode"] == "routing"
    assert params["q_min"] == 0.5 and params["tau_change"] == 0.2
    assert params["confidence_threshold"] is None
    assert params["scale"] == 20


def test_parse_params_explicit_dates_are_exclusive_end():
    params = fusion.parse_params({
        "aoi": AOI, "fusion_mode": "weighted",
        "from_start_date": "2023-01-01", "from_end_date": "2023-03-31",
        "to_start_date": "2024-01-01", "to_end_date": "2024-03-31",
    })
    assert params["t0"] == ("2023-01-01", "2023-04-01")
    assert params["fusion_mode"] == "weighted"


def test_parse_params_clamps_to_sentinel_era_and_today():
    params = fusion.parse_params({"aoi": AOI, "from_year": 2017, "to_year": date.today().year})
    assert params["t0"][0] == "2017-03-28"
    assert params["t1"][1] == (date.today() + timedelta(days=1)).isoformat()


@pytest.mark.parametrize("payload, message", [
    ({"aoi": AOI, "from_year": 2016, "to_year": 2024}, "kosong"),
    ({"from_year": 2020, "to_year": 2024}, "aoi"),
    ({"aoi": AOI, "from_year": 2024, "to_year": 2020}, "lebih awal"),
    ({"aoi": AOI, "from_year": 2020, "to_year": 2024, "fusion_mode": "average"}, "fusion_mode"),
    ({"aoi": AOI, "from_year": 2020, "to_year": 2024, "q_min": 1.5}, "q_min"),
    ({"aoi": AOI, "from_year": 2020, "to_year": 2024, "tau_change": -0.1}, "tau_change"),
    ({"aoi": AOI, "from_year": 2020}, "to_year"),
])
def test_parse_params_rejects_invalid(payload, message):
    with pytest.raises(AnalysisError) as error:
        fusion.parse_params(payload)
    assert message in str(error.value)
    assert error.value.status_code in {400, 422}


def test_feature_subsets_share_terrain_and_split_sensors():
    joint, optical, sar = (fusion.PATH_FEATURES[k] for k in ("joint", "optical", "sar"))
    assert set(optical) | set(sar) == set(joint)
    assert not set(fusion.SAR_FEATURES) & set(optical)
    assert not set(fusion.OPTICAL_FEATURES) & set(sar)
    assert set(fusion.AUX_FEATURES) <= set(optical) & set(sar)


def test_routes_and_job_registration():
    assert fusion.ROUTES == {0: "no_data", 1: "optical_dominant", 2: "sar_dominant", 3: "joint_fusion"}
    assert [item["value"] for item in fusion.ROUTE_LEGEND] == [1, 2, 3]
    assert "landcover_fusion" in analysis_jobs._ALLOWED_JOB_TYPES
    assert "land_cover_change" in FEATURE_CODES
