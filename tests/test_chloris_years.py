"""Chloris vintages must match actual rasters and never fall back to another year."""
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.routes import carbon as carbon_routes
from app.api.routes import chloris as chloris_routes
from app.providers import local_raster_provider as raster_provider
from app.registries.carbon_dataset_registry import get_dataset_meta
from app.services import carbon_service, chloris_service as chloris
from app.services.gee_common import AnalysisError


@pytest.fixture(autouse=True)
def chloris_settings(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("CHLORIS_"):
            monkeypatch.delenv(key)
    settings = SimpleNamespace(
        chloris_base_url="https://app.chloris.earth", chloris_data_path="",
        chloris_organization_id="", chloris_reporting_unit_id="",
        chloris_id_token="", chloris_refresh_token="",
        chloris_stock_url="", chloris_agb_stock_url="",
        chloris_stock_2024_url="", chloris_stock_2025_url="",
        chloris_stock_url_template="", chloris_stock_years="",
        chloris_stock_year=None, chloris_stock_resolution_m=30,
    )
    monkeypatch.setattr(chloris, "get_settings", lambda: settings)
    monkeypatch.setattr(chloris, "is_planetary_computer_available", lambda: True)
    chloris._DOWNLOADS_CACHE.clear()
    return settings


def stock(year, url=None):
    return {"urlFormat": "stock", "format": "tif", "date": f"{year}-01-01",
            "url": url or f"https://app.chloris.earth/data/stock_{year}.tif"}


def test_licensed_index_never_substitutes_another_year():
    with pytest.raises(chloris.ChlorisUnavailableError, match="2025 tidak tersedia"):
        chloris._select_download({"downloads": [stock(2024), stock(2019)]}, "stock", 2025)


@pytest.mark.parametrize("year", [2024, 2025])
def test_licensed_index_resolves_exact_requested_year(year, monkeypatch, chloris_settings):
    chloris_settings.chloris_data_path = "https://app.chloris.earth/data/site"
    monkeypatch.setattr(chloris, "_request_json", lambda _url: {"downloads": [stock(2024), stock(2025)]})
    download = chloris.resolve_chloris_download(year=year)
    assert download.metadata["year"] == year
    assert str(year) in download.url
    assert download.metadata["source"] == "chloris_reporting_unit"


def test_index_is_cached_between_catalog_and_resolver(monkeypatch, chloris_settings):
    chloris_settings.chloris_data_path = "https://app.chloris.earth/data/site"
    calls = []
    monkeypatch.setattr(chloris, "_request_json", lambda url: calls.append(url) or {"downloads": [stock(2025)]})
    assert 2025 in chloris.chloris_availability()["licensed_available_years"]
    assert chloris.resolve_chloris_download(year=2025).metadata["year"] == 2025
    assert len(calls) == 1


def test_unconfigured_server_only_offers_public_vintages():
    meta = get_dataset_meta("CHLORIS_AGB_STOCK")
    assert meta["available_years"] == list(range(2003, 2020))
    assert meta["licensed_available_years"] == []
    assert meta["year"] == 2019
    assert meta["resolution"] == 4633
    with pytest.raises(AnalysisError, match="Chloris 2025 belum tersedia"):
        carbon_service.get_direct_carbon_reference_layer("CHLORIS_AGB_STOCK", 2025)


def test_dated_urls_update_catalog_and_keep_public_resolution(chloris_settings):
    chloris_settings.chloris_stock_2024_url = "https://storage.example/stock_2024.tif"
    chloris_settings.chloris_stock_2025_url = "https://storage.example/stock_2025.tif"
    meta = get_dataset_meta("CHLORIS_AGB_STOCK")
    assert meta["year"] == 2025
    assert meta["year_range"] == [2003, 2025]
    assert meta["licensed_available_years"] == [2024, 2025]
    assert 2020 not in meta["available_years"]
    assert meta["resolution"] == 30
    assert get_dataset_meta("CHLORIS_AGB_STOCK", 2018)["resolution"] == 4633


def test_template_requires_explicit_available_years(chloris_settings):
    chloris_settings.chloris_stock_url_template = "https://storage.example/stock_{year}.tif"
    assert chloris.chloris_availability()["licensed_available_years"] == []
    chloris_settings.chloris_stock_years = "2024,2025"
    assert chloris.resolve_chloris_download(year=2025).url.endswith("stock_2025.tif")
    assert chloris.chloris_availability()["licensed_available_years"] == [2024, 2025]


def test_undated_fixed_url_cannot_be_mislabeled(chloris_settings):
    chloris_settings.chloris_stock_url = "https://storage.example/fixed.tif"
    with pytest.raises(chloris.ChlorisUnavailableError, match="2025 belum tersedia"):
        chloris.resolve_chloris_download(year=2025)
    chloris_settings.chloris_stock_year = 2024
    assert chloris.resolve_chloris_download(year=2024).metadata["year"] == 2024
    with pytest.raises(chloris.ChlorisUnavailableError, match="2025 belum tersedia"):
        chloris.resolve_chloris_download(year=2025)


def test_latest_selection_accepts_explicit_year_fields():
    entries = [{"year": year, "urlFormat": "stock", "url": f"https://example/{year}.tif"}
               for year in [2024, 2025]]
    assert chloris._select_download(entries, "stock", None)["year"] == 2025


def test_carbon_layer_uses_licensed_source_even_without_earth_engine(chloris_settings, monkeypatch):
    chloris_settings.chloris_stock_2025_url = "https://storage.example/private_2025.tif"

    def no_public_fallback(**_kwargs):
        raise AssertionError("Licensed 2025 must not be replaced by public 2019")

    monkeypatch.setattr(chloris, "resolve_planetary_computer_tile_url", no_public_fallback)
    layer = carbon_service.get_direct_carbon_reference_layer(
        "CHLORIS_AGB_STOCK", 2025, use_public_chloris_tiles=True,
    )
    assert layer["year"] == layer["effective_year"] == 2025
    assert layer["resolution"] == 30
    assert layer["source"] == "direct_env_url"
    assert layer["tile_url"].startswith("/api/carbon/chloris/reference-tiles/")
    assert "private_2025" not in str(layer)
    assert layer["unit"] == "Mg C/ha"


def test_bearer_credentials_only_go_to_chloris_origin(chloris_settings):
    chloris_settings.chloris_id_token = "test-id-token"
    assert chloris.chloris_raster_http_options("https://app.chloris.earth/data/file.tif") == {
        "GDAL_HTTP_HEADERS": "Authorization: Bearer test-id-token",
        "CPL_VSIL_CURL_AUTHORIZATION_HEADER_ALLOWED_IF_REDIRECT": "IF_SAME_HOST",
    }
    assert chloris.chloris_raster_http_options("https://storage.example/file.tif") == {}
    assert chloris.chloris_raster_http_options("http://app.chloris.earth/data/file.tif") == {}


def test_licensed_tiles_require_authentication():
    app = FastAPI()
    app.include_router(carbon_routes.router)
    for dependency in carbon_routes.router.dependencies:
        app.dependency_overrides[dependency.dependency] = lambda: None
    with TestClient(app) as client:
        assert client.get("/carbon/chloris/reference-tiles/8/206/132.png?year=2025").status_code == 401


def test_layer_and_diagnostics_api_work_without_ee_and_hide_private_urls(chloris_settings):
    chloris_settings.chloris_stock_2025_url = "https://storage.example/private_2025.tif"
    app = FastAPI()
    app.include_router(carbon_routes.router)
    app.include_router(chloris_routes.router)
    for router in (carbon_routes.router, chloris_routes.router):
        for dependency in router.dependencies:
            app.dependency_overrides[dependency.dependency] = lambda: None
    app.dependency_overrides[carbon_routes.get_current_app_viewer] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as client:
        layer = client.get("/carbon/reference-layers/CHLORIS_AGB_STOCK?year=2025")
        assert layer.status_code == 200
        assert layer.json()["effective_year"] == 2025
        status = client.get("/carbon/chloris/status?year=2025")
        assert status.status_code == 200
        assert status.json()["licensed_available_years"] == [2025]
        assert status.json()["resolved_download"]["year"] == 2025
        assert "private_2025" not in status.text + layer.text


def test_licensed_aoi_totals_only_cover_valid_pixels(chloris_settings, monkeypatch):
    from affine import Affine

    chloris_settings.chloris_stock_2025_url = "https://storage.example/stock_2025.tif"
    labels = np.array([[47, np.nan], [47, np.nan]])
    monkeypatch.setattr(raster_provider, "GridSpec", lambda *_args, **_kwargs: SimpleNamespace(
        transform=Affine(30, 0, 0, 0, -30, 0), crs="EPSG:32749", shape=(2, 2),
    ))
    monkeypatch.setattr(raster_provider, "load_carbon_reference_to_grid", lambda *_args, **_kwargs: (
        labels, {"label_year": 2025, "source": "direct_env_url"},
    ))
    monkeypatch.setattr(carbon_service, "_local_aoi_areas_ha", lambda *_args: (50, 50, 50))
    monkeypatch.setattr(carbon_service.config_service, "get_analysis_defaults", lambda _db: {
        "cloud_threshold": 10, "carbon_vis_min": 0, "carbon_vis_max": 250,
        "carbon_vis_palette": ["ffffff", "006d2c"], "carbon_co2_factor": 3.67,
        "carbon_scale": 300, "max_pixels": 1_000_000,
    })
    result = carbon_service.analyze_carbon(None, {
        "aoi": {"west": 110, "south": -7, "east": 111, "north": -6},
        "reference_dataset": "CHLORIS_AGB_STOCK", "dataset_year": 2025,
        "reference_only": True,
    })
    assert result["carbon_reference"]["year"] == 2025
    assert result["carbon_reference"]["statistics"]["mean"] == 47
    assert result["area_info"]["calculation_area_ha"] == pytest.approx(0.18)
    assert result["area_info"]["total_carbon_tons"] == pytest.approx(8.46)
    assert result["model_info"]["analysis_year"] == 2025


def test_raster_grid_and_png_use_actual_stock_pixels(tmp_path, chloris_settings, monkeypatch):
    import rasterio
    from rasterio.transform import from_bounds

    bounds = raster_provider._xyz_bounds_wgs84(8, 206, 132)
    path = tmp_path / "stock_2025.tif"
    raw = np.full((32, 32), 100, dtype=np.float32)
    with rasterio.open(path, "w", driver="GTiff", width=32, height=32, count=1,
                       dtype="float32", crs="EPSG:4326", transform=from_bounds(*bounds, 32, 32), nodata=-9999) as src:
        src.write(raw, 1)
    chloris_settings.chloris_stock_2025_url = str(path)
    grid = raster_provider.GridSpec(bounds, resolution_m=5000)
    data, info = raster_provider.load_carbon_reference_to_grid("CHLORIS_AGB_STOCK", grid, 2025)
    assert info["label_year"] == 2025
    assert info["source"] == "direct_env_url"
    np.testing.assert_allclose(data[np.isfinite(data)], 47, rtol=1e-5)
    monkeypatch.setattr(raster_provider, "_read_cached_carbon_tile", lambda _key: None)
    monkeypatch.setattr(raster_provider, "_write_cached_carbon_tile", lambda *_args: None)
    content = raster_provider.render_carbon_reference_tile("CHLORIS_AGB_STOCK", 8, 206, 132, 2025)
    assert content is not None
    image = Image.open(BytesIO(content))
    assert image.size == (256, 256)
    assert image.getpixel((128, 128))[3] == 220
