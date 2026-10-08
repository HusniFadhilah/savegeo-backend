import pytest

from app.services import vegetation_service
from app.services.gee_common import AnalysisError


class _InfoValue:
    def __init__(self, value):
        self.value = value

    def getInfo(self):
        return self.value


class _MetricImage:
    def __init__(self, name):
        self.name = name

    def rename(self, name):
        self.name = name
        return self


class _Composite:
    def bandNames(self):
        return _InfoValue(["B2", "B3", "B4", "B8", "B11"])

    def select(self, bands):
        return _MetricImage(bands[0])


class _Stack:
    def __init__(self, metric_images, reductions):
        self.metric_images = metric_images
        self.reductions = reductions

    def reduceRegion(self, **kwargs):
        self.reductions.append({**kwargs, "metric_names": [image.name for image in self.metric_images]})
        return _InfoValue({image.name: 0.25 for image in self.metric_images})


class _Reducer:
    @staticmethod
    def mean():
        return "mean"


def _patch_monthly_ee(monkeypatch, *, fail_month=None, empty_month=None):
    reductions = []
    include_quality_values = []

    def composite_for_period(*args, include_quality=True, **kwargs):
        month = args[3]
        include_quality_values.append(include_quality)
        if month == fail_month:
            raise RuntimeError("simulated Earth Engine failure")
        if month == empty_month:
            return None, None, None, None, 0, None
        return _Composite(), None, None, None, 1, None

    monkeypatch.setattr(vegetation_service, "_composite_for_period", composite_for_period)
    monkeypatch.setattr(vegetation_service, "create_geometry_from_payload", lambda payload: "aoi")
    monkeypatch.setattr(vegetation_service, "resolve_satellite", lambda key: key or "sentinel2")
    monkeypatch.setattr(vegetation_service, "get_satellite_meta", lambda db, key: {"name": "Sentinel-2", "provider": "ESA", "resolution_m": 10})
    monkeypatch.setattr(vegetation_service.config_service, "get_analysis_defaults", lambda db: {"cloud_threshold": 40, "veg_scale": 10, "max_pixels": 1_000_000})
    monkeypatch.setattr(vegetation_service, "calculate_index", lambda image, name: _MetricImage(name))
    monkeypatch.setattr(vegetation_service.ee, "Reducer", _Reducer, raising=False)
    monkeypatch.setattr(
        vegetation_service.ee.Image,
        "cat",
        staticmethod(lambda images: _Stack(images, reductions)),
    )
    return reductions, include_quality_values


def test_monthly_series_batches_indices_and_bands_and_keeps_empty_months(monkeypatch):
    reductions, include_quality = _patch_monthly_ee(monkeypatch, empty_month=2)

    result = vegetation_service.analyze_timeseries(None, {
        "aoi": {"type": "FeatureCollection", "features": []},
        "year": 2026,
        "interval": "monthly",
        "start_month": 1,
        "end_month": 3,
        "indices": ["NDVI", "EVI"],
        "bands": ["B2", "B11"],
        "satellite": "sentinel2",
    })

    assert result["periods"] == ["2026-01", "2026-02", "2026-03"]
    assert result["indices"] == ["NDVI", "EVI"]
    assert result["bands"] == ["B2", "B11"]
    assert result["series"]["NDVI"]["data"] == [
        {"period": "2026-01", "value": 0.25},
        {"period": "2026-02", "value": None},
        {"period": "2026-03", "value": 0.25},
    ]
    assert len(reductions) == 2
    assert all(reduction["metric_names"] == ["NDVI", "EVI", "B2", "B11"] for reduction in reductions)
    assert all(reduction["scale"] == 20 and reduction["tileScale"] == 4 for reduction in reductions)
    assert include_quality == [False, False, False]


def test_monthly_series_keeps_other_months_when_one_earth_engine_period_fails(monkeypatch):
    reductions, _ = _patch_monthly_ee(monkeypatch, fail_month=2)

    result = vegetation_service.analyze_timeseries(None, {
        "aoi": {"type": "FeatureCollection", "features": []},
        "year": 2026,
        "start_month": 1,
        "end_month": 3,
        "indices": ["NDVI", "EVI"],
        "bands": ["B2"],
    })

    assert result["period_errors"] == ["2026-02"]
    assert result["series"]["B2"]["data"][1] == {"period": "2026-02", "value": None}
    assert len(reductions) == 2


def test_monthly_series_rejects_invalid_month_range():
    with pytest.raises(AnalysisError, match="Rentang bulan"):
        vegetation_service.analyze_timeseries(None, {
            "aoi": {"type": "FeatureCollection", "features": []},
            "year": 2026,
            "start_month": 10,
            "end_month": 2,
            "indices": ["NDVI"],
        })
