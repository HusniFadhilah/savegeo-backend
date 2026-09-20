"""GeoTIFF metadata must describe the product year, not only the request."""

import pytest

from app.services import download_service
from app.services.gee_common import AnalysisError


class _Aoi:
    def bounds(self):
        return self

    def getInfo(self):
        return {"coordinates": [[100, -5], [101, -5], [101, -4], [100, -4]]}


class _Image:
    def __init__(self, error=None):
        self.error = error

    def getDownloadURL(self, _params):
        if self.error:
            raise self.error
        return "https://example.test/export.tif"


@pytest.mark.parametrize("error", [None, RuntimeError("private-export-token")])
def test_landcover_export_reports_effective_year_and_hides_upstream_error(monkeypatch, error):
    monkeypatch.setattr(download_service.config_service, "get_analysis_defaults", lambda _db: {"cloud_threshold": 20})
    monkeypatch.setattr(download_service, "create_geometry_from_payload", lambda _spec: _Aoi())
    monkeypatch.setattr(
        download_service,
        "get_landcover_image",
        lambda *_args: (_Image(error), {"year": 2021}),
    )
    payload = {"aoi": {"west": 100, "south": -5, "east": 101, "north": -4}, "layer_type": "landcover", "dataset": "ESA_WorldCover", "year": 2026}

    if error:
        with pytest.raises(AnalysisError) as raised:
            download_service.download_geotiff(object(), payload)
        assert "private-export-token" not in str(raised.value)
    else:
        result = download_service.download_geotiff(object(), payload)
        assert result["requested_year"] == 2026
        assert result["effective_year"] == result["year"] == 2021
