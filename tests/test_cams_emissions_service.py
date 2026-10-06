from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import cams_emissions_service as cams


def test_capabilities_report_missing_secrets_without_returning_secret_values():
    secret = "test-only-cams-secret"
    settings = SimpleNamespace(
        cams_ads_api_url="https://ads.atmosphere.copernicus.eu/api",
        cams_ads_api_key=secret,
        cams_ads_terms_accepted=False,
    )

    result = cams.get_capabilities(settings)

    serialized = json.dumps(result)
    assert result["status"] == "needs_terms_acceptance"
    assert result["credentials_configured"] is True
    assert result["data_available"] is False
    assert secret not in serialized
    assert "gfas" in {product["id"] for product in result["products"]}
    assert result["products"][0]["variables"]["co2"]["unit"] == "kg m-2 s-1"


def test_capabilities_reject_non_official_ads_host():
    result = cams.get_capabilities(
        SimpleNamespace(
            cams_ads_api_url="https://example.invalid/api",
            cams_ads_api_key="secret",
            cams_ads_terms_accepted=True,
        )
    )
    assert result["status"] == "invalid_configuration"
    assert result["data_available"] is False


def test_ads_archive_adapter_uses_official_client_contract_with_a_mock(tmp_path):
    calls = []

    class FakeClient:
        def retrieve(self, dataset_id, request, target):
            calls.append((dataset_id, request, target))

    adapter = cams.CdsApiArchiveAdapter(FakeClient())
    target = tmp_path / "field.grib"
    request = {"variable": ["wildfire_flux_of_carbon_dioxide_co2"], "date": ["2026-10-06"]}

    result = adapter.retrieve(cams.GFAS_DATASET_ID, request, target)

    assert result == target
    assert calls == [(cams.GFAS_DATASET_ID, request, str(target))]


def test_ads_adapter_redacts_provider_exceptions_and_checks_terms_first():
    secret = "test-only-cams-secret"

    class BrokenClient:
        def retrieve(self, *_args):
            raise RuntimeError(f"request failed for key={secret}")

    with pytest.raises(cams.CamsRequestError) as captured:
        cams.CdsApiArchiveAdapter(BrokenClient()).retrieve(
            cams.GFAS_DATASET_ID, {"date": ["2026-10-06"]}, "field.grib"
        )
    assert captured.value.code == "ads_request_failed"
    assert secret not in str(captured.value)
    assert captured.value.__suppress_context__ is True

    with pytest.raises(cams.CamsRequestError) as blocked:
        cams.create_ads_archive_adapter(
            SimpleNamespace(
                cams_ads_api_url="https://ads.atmosphere.copernicus.eu/api",
                cams_ads_api_key=secret,
                cams_ads_terms_accepted=False,
            ),
            client_factory=lambda **_kwargs: BrokenClient(),
        )
    assert blocked.value.code == "terms_not_accepted"


def test_request_validation_enforces_version_coverage_and_utc():
    request = cams.validate_request("gfas", "1.4.2", "co2", "2026-10-06T03:00:00+07:00")
    assert request["valid_at"] == "2026-10-05T20:00:00Z"

    with pytest.raises(cams.CamsRequestError, match="di luar rentang"):
        cams.validate_request("gfas", "1.2", "co2", "2026-01-01T00:00:00Z")
    with pytest.raises(cams.CamsRequestError, match="menyertakan zona waktu"):
        cams.validate_request("gfas", "1.4.2", "co2", "2026-10-06T03:00:00")
    with pytest.raises(cams.CamsRequestError, match="tidak tersedia"):
        cams.validate_request("gfas", "1.4.2", "unknown", "2026-10-06T00:00:00Z")


def test_ifs_valid_time_requires_hourly_utc_boundary():
    valid = cams.validate_request("ifs_cams", "current", "pm25", "2026-10-06T03:00:00Z")
    assert valid["product"] == "ifs_cams"
    with pytest.raises(cams.CamsRequestError, match="langkah 1 jam"):
        cams.validate_request("ifs_cams", "current", "pm25", "2026-10-06T03:30:00Z")
    with pytest.raises(cams.CamsRequestError, match="langkah 3 jam"):
        cams.validate_request("ifs_cams", "current", "co", "2026-10-06T02:00:00Z")


def test_geodesic_cell_area_is_finite_and_validated():
    area = cams.geodesic_cell_area_m2(113.0, -1.0, 113.1, -0.9)
    assert 100_000_000 < area < 130_000_000
    with pytest.raises(cams.CamsRequestError):
        cams.geodesic_cell_area_m2(180, -1, -180, 0)


def test_flux_integration_uses_clipped_area_and_actual_duration():
    cell = cams.FluxCell(west=113, south=-1, east=114, north=0, value=2e-6)
    aoi = {
        "type": "Polygon",
        "coordinates": [[[113, -1], [113.5, -1], [113.5, 0], [113, 0], [113, -1]]],
    }
    clipped_area = cams.clipped_cell_area_m2(cell, aoi)
    result = cams.integrate_flux_cells([cell], duration_seconds=3600, aoi_geojson=aoi)

    assert result["valid_cell_count"] == 1
    assert result["integrated_mass_kg"] == pytest.approx(2e-6 * clipped_area * 3600, rel=1e-12)
    assert result["max_flux_kg_m2_s"] == 2e-6
    assert result["duration_seconds"] == 3600


def test_region_aggregation_uses_each_boundary_and_preserves_separate_totals():
    cells = [
        cams.FluxCell(113.0, -1.0, 113.1, -0.9, 1e-6),
        cams.FluxCell(113.1, -1.0, 113.2, -0.9, 2e-6),
    ]
    west_region = {
        "type": "Polygon",
        "coordinates": [[[113, -1], [113.1, -1], [113.1, -0.9], [113, -0.9], [113, -1]]],
    }
    east_region = {
        "type": "Polygon",
        "coordinates": [[[113.1, -1], [113.2, -1], [113.2, -0.9], [113.1, -0.9], [113.1, -1]]],
    }
    west = cams.integrate_flux_cells(cells, 3600, west_region)
    east = cams.integrate_flux_cells(cells, 3600, east_region)
    assert west["max_flux_kg_m2_s"] == 1e-6
    assert east["max_flux_kg_m2_s"] == 2e-6
    assert east["integrated_mass_kg"] == pytest.approx(west["integrated_mass_kg"] * 2, rel=0.02)


def test_nodata_is_missing_and_never_counted_as_zero_mass():
    cells = [
        cams.FluxCell(113, -1, 113.1, -0.9, None),
        cams.FluxCell(113.1, -1, 113.2, -0.9, -9999),
    ]
    result = cams.integrate_flux_cells(cells, 86400, nodata=-9999)
    assert result["valid_cell_count"] == 0
    assert result["missing_cell_count"] == 2
    assert result["integrated_mass_kg"] is None
    assert result["mean_flux_kg_m2_s"] is None


def test_frp_density_is_area_integrated_to_watts_without_flux_time_integral():
    cell = cams.FluxCell(113, -1, 113.1, -0.9, 0.5)
    area = cams.clipped_cell_area_m2(cell)
    result = cams.aggregate_frp_density([cell])
    assert result["total_frp_w"] == pytest.approx(area * 0.5, rel=1e-12)
    assert result["max_frp_density_w_m2"] == 0.5


def test_extreme_or_negative_flux_values_fail_closed():
    extreme = cams.FluxCell(113, -1, 114, 0, 1e308)
    negative = cams.FluxCell(113, -1, 114, 0, -1e-6)
    with pytest.raises(cams.CamsRequestError, match="rentang numerik"):
        cams.integrate_flux_cells([extreme], 3600)
    with pytest.raises(cams.CamsRequestError, match="tidak boleh negatif"):
        cams.integrate_flux_cells([negative], 3600)
    assert math.isfinite(cams.geodesic_cell_area_m2(113, -1, 114, 0))


def test_cache_key_includes_time_region_and_visual_parameters():
    request = {"product": "gfas", "version": "1.4.2", "variable": "co2", "valid_at": "2026-10-06T00:00:00Z"}
    first = cams.build_cache_key(request, "kalimantan", {"scale": "linear"})
    assert first == cams.build_cache_key(request, "kalimantan", {"scale": "linear"})
    assert first != cams.build_cache_key(request, "province-62", {"scale": "linear"})
    assert first != cams.build_cache_key(request, "kalimantan", {"scale": "log"})


def test_fixture_grid_summary_preserves_provenance_and_integrates_flux():
    field = cams.GridField(
        product="gfas",
        version="1.4.2",
        variable="co",
        valid_at=datetime(2026, 10, 6, 0, tzinfo=UTC),
        unit="kg m-2 s-1",
        cells=(cams.FluxCell(113, -1, 113.1, -0.9, 1e-6),),
    )

    class FixtureProvider:
        def read_field(self, request):
            assert request["product"] == "gfas"
            assert request["valid_at"] == "2026-10-06T00:00:00Z"
            return field

    result = cams.summarize_request(
        FixtureProvider(),
        {"product": "gfas", "version": "1.4.2", "variable": "co", "valid_at": "2026-10-06T00:00:00Z"},
        duration_seconds=3600,
    )
    assert result["request"]["version"] == "1.4.2"
    assert result["request"]["variable"] == "co"
    assert result["unit"] == "kg"
    assert result["method"] == "flux × clipped cell area × interval seconds"


def test_fixture_provider_empty_field_is_not_reported_as_zero():
    class EmptyProvider:
        def read_field(self, request):
            return None

    with pytest.raises(cams.CamsRequestError) as captured:
        cams.summarize_request(
            EmptyProvider(),
            {"product": "gfas", "version": "1.4.2", "variable": "co", "valid_at": "2026-10-06T00:00:00Z"},
            duration_seconds=3600,
        )
    assert captured.value.code == "data_unavailable"
    assert captured.value.status_code == 503


def test_fixture_provider_provenance_mismatch_is_rejected():
    field = cams.GridField(
        product="gfas",
        version="1.4.2",
        variable="co",
        valid_at=datetime(2026, 10, 6, 1, tzinfo=UTC),
        unit="kg m-2 s-1",
        cells=(cams.FluxCell(113, -1, 113.1, -0.9, 1e-6),),
    )

    class WrongTimeProvider:
        def read_field(self, request):
            return field

    with pytest.raises(cams.CamsRequestError) as captured:
        cams.summarize_request(
            WrongTimeProvider(),
            {"product": "gfas", "version": "1.4.2", "variable": "co", "valid_at": "2026-10-06T00:00:00Z"},
            duration_seconds=3600,
        )
    assert captured.value.code == "provider_provenance_mismatch"


def test_flux_duration_must_match_declared_dataset_step():
    daily = cams.GridField(
        product="gfas",
        version="1.2",
        variable="co2",
        valid_at=datetime(2025, 12, 31, 0, tzinfo=UTC),
        unit="kg m-2 s-1",
        cells=(cams.FluxCell(113, -1, 113.1, -0.9, 1e-6),),
        temporal_step="daily",
    )
    assert cams.summarize_field(daily, 86_400)["duration_seconds"] == 86_400
    with pytest.raises(cams.CamsRequestError, match="tidak cocok"):
        cams.summarize_field(daily, 3600)
