from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import carbon_extensions as service
from app.services import raster_batch_export as exports
from app.services.gee_common import AnalysisError


@pytest.mark.parametrize("value", [None, "", -1, float("nan"), float("inf"), True])
def test_stock_scenarios_require_measured_finite_coefficients(value):
    with pytest.raises(AnalysisError):
        service.required_number({"density": value}, "density")


@pytest.mark.parametrize("kind,band,minimum", [("npp", "Npp", -30000), ("gpp", "Gpp", 0)])
def test_modis_conversion_preserves_units_and_negative_npp(kind, band, minimum):
    with patch.object(service, "ee") as ee:
        ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value.size.return_value.getInfo.return_value = 1
        image, meta = service.build_extension_image({"extension": kind, "year": 2024}, MagicMock())
        raw = ee.Image.return_value.select.return_value
        ee.Image.return_value.select.assert_called_once_with(band)
        raw.gte.assert_called_once_with(minimum)
        raw.updateMask.return_value.multiply.assert_called_once_with(0.001)
        assert meta["unit"] == "Mg C/ha/tahun"
        assert meta["quantity"] == "productivity"
        assert image is not None


def test_unpublished_productivity_year_is_not_replaced_with_a_different_year():
    with patch.object(service, "ee") as ee:
        ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value.size.return_value.getInfo.return_value = 0
        ee.ImageCollection.return_value.aggregate_max.return_value.getInfo.return_value = 1735689600000
        with pytest.raises(AnalysisError, match="2025"):
            service.build_extension_image({"extension": "npp", "year": 2026}, MagicMock())


def test_bluecarbon_habitat_and_depth_are_explicit_and_coefficients_are_added():
    settings = {
        "source": "Field inventory 2024",
        "habitat": "aoi_habitat",
        "habitat_confirmed": True,
        "agb_density": 30,
        "bgb_density": 10,
        "soc_density": 150,
        "soil_depth_cm": 100,
    }
    with patch.object(service, "ee") as ee:
        _, meta = service.build_extension_image(
            {"extension": "bluecarbon", "year": 2024, "extension_settings": settings}, MagicMock()
        )
        assert ee.Image.constant.call_args_list[-1].args[0] == 190
        assert "100 cm" in meta["description"]
        assert "Field inventory" in meta["description"]
        assert meta["quantity"] == "stock_scenario"
    settings["habitat_confirmed"] = False
    with pytest.raises(AnalysisError, match="Konfirmasi"):
        service.build_extension_image(
            {"extension": "bluecarbon", "year": 2024, "extension_settings": settings}, MagicMock()
        )


def test_full_resolution_export_keeps_requested_grid_and_owner_bound_status(monkeypatch):
    settings = SimpleNamespace(
        gee_export_bucket="test-export-bucket", jwt_secret_key="a-long-test-secret-key-for-export-status-1234"
    )
    monkeypatch.setattr(exports, "get_settings", lambda: settings)
    with patch.object(exports, "ee") as ee:
        ee.batch.Export.image.toCloudStorage.return_value.id = "task-123"
        result = exports.start_export(MagicMock(), MagicMock(), "kalimantan", 10, 2026, 2026, "admin:1")
        options = ee.batch.Export.image.toCloudStorage.call_args.kwargs
        assert options["scale"] == 10
        assert options["fileDimensions"] == 8192
        assert options["formatOptions"]["cloudOptimized"] is True
        assert result["scale_adjusted"] is False
        ee.data.getTaskStatus.return_value = [{"state": "COMPLETED"}]
        assert exports.export_status(result["task_token"], "admin:1")["state"] == "COMPLETED"
        with pytest.raises(AnalysisError):
            exports.export_status(result["task_token"], "user:2")


def test_full_resolution_export_reports_missing_storage_configuration(monkeypatch):
    monkeypatch.setattr(exports, "get_settings", lambda: SimpleNamespace(gee_export_bucket=""))
    with pytest.raises(AnalysisError, match="GEE_EXPORT_BUCKET"):
        exports.start_export(MagicMock(), MagicMock(), "test", 10, 2026, 2026, "admin:1")


def test_ipcc_preset_uses_documented_carbon_factors_and_retains_reference_vintage():
    settings = {
        "preset": "tropical_rainforest",
        "domain_confirmed": True,
        "density": 999,
        "source": "incorrect",
    }
    with patch.object(service, "ee"), patch.object(service, "annual_labels"):
        _, meta = service.build_extension_image(
            {"extension": "deadwood", "year": 2024, "extension_settings": settings}, MagicMock()
        )
    assert meta["settings"]["density"] == 14.8
    assert meta["reference_year"] == 2019
    assert "IPCC 2019" in meta["source"]
    assert meta["settings"]["mask"] == "trees"
    settings["domain_confirmed"] = False
    with pytest.raises(AnalysisError, match="konfirmasikan"):
        service.build_extension_image(
            {"extension": "deadwood", "year": 2024, "extension_settings": settings}, MagicMock()
        )
