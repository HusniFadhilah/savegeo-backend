import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from app.services import cams_archive_service as service
from app.services.cams_emissions_service import CamsRequestError
from scripts.sync_cams_archive import cell_areas, geometry, ingest_file, statistics


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(cams_archive_dir=str(tmp_path)))
    directory = tmp_path / "test-fire"
    directory.mkdir()
    identifier = "a" * 24
    path = directory / (identifier + ".tif")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=2,
        width=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-180, 85, 180, 85),
        nodata=np.nan,
    ) as dst:
        dst.write(np.array([[1, 0], [np.nan, 2]], dtype="float32"), 1)
    frame = {
        "id": identifier,
        "filename": path.name,
        "sha256": "b" * 64,
        "valid_at": "2026-10-03T00:00:00Z",
        "version": "current",
        "summary": {"area_weighted_mean": 0.5},
        "regions": [],
        "provenance": {"source_unit": "1"},
    }
    manifest = {
        "schema_version": 1,
        "event_slug": "test-fire",
        "updated_at": "2026-10-07T00:00:00Z",
        "layers": {
            "organic_aod": {
                "frames": [frame],
                "maximum": 2,
                "palette": service.LAYER_DEFINITIONS["organic_aod"]["palette"],
            }
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory, identifier


def test_public_manifest_hides_server_paths_and_detects_missing_rasters(archive):
    directory, identifier = archive
    payload = service.public_archive("test-fire")
    assert payload["layers"]["organic_aod"]["rendering"]["maximum"] == 1
    assert payload["layers"]["organic_aod"]["maximum"] == 2
    assert "filename" not in payload["layers"]["organic_aod"]["frames"][0]
    (directory / (identifier + ".tif")).unlink()
    with pytest.raises(CamsRequestError, match="lengkap"):
        service.public_archive("test-fire")


def test_archive_rejects_path_traversal():
    with pytest.raises(CamsRequestError):
        service.event_directory("../private")


def test_tiles_are_png_with_mask_and_match_the_legend(archive):
    _, identifier = archive
    png = service.tile("test-fire", "organic_aod", identifier, 0, 0, 0)
    image = np.asarray(Image.open(io.BytesIO(png)))
    assert image.shape == (256, 256, 4)
    assert (image[:, :, 3] == 0).any()
    assert (image[:, :, 3] == 255).any()
    visible = image[image[:, :, 3] == 255]
    expected_max = [64, 0, 107]
    assert np.any(np.all(visible[:, :3] == expected_max, axis=1))
    # Smooth aerosol transitions and low-value alpha fading, not two enlarged
    # source pixel colours or a fully opaque wash over the basemap.
    assert len(np.unique(image[image[:, :, 3] > 0, :3], axis=0)) > 20
    assert ((image[:, :, 3] > 0) & (image[:, :, 3] < 230)).any()


def test_point_reads_native_cells_including_zero_and_reports_missing_data(archive):
    _, identifier = archive
    point = service.point("test-fire", "organic_aod", identifier, 60, -20)
    assert point["data_available"] is True
    assert point["value"] == 1
    assert point["center"] == {"lat": 42.5, "lon": -90}
    assert point["valid_at"] == "2026-10-03T00:00:00Z"
    assert point["mass_tonnes"] is None
    zero = service.point("test-fire", "organic_aod", identifier, 60, 20)
    assert zero["data_available"] is True and zero["value"] == 0
    assert service.point("test-fire", "organic_aod", identifier, -10, 20)["value"] == 2
    missing = service.point("test-fire", "organic_aod", identifier, -10, -20)
    assert missing["reason"] == "nodata" and missing["value"] is None
    outside = service.point("test-fire", "organic_aod", identifier, -89, 20)
    assert outside["reason"] == "outside_coverage" and outside["center"] is None


@pytest.mark.parametrize("lat,lon", [(float("nan"), 0), (0, float("inf")), (91, 0), (0, -181)])
def test_point_rejects_invalid_coordinates(archive, lat, lon):
    with pytest.raises(CamsRequestError, match="Koordinat"):
        service.point("test-fire", "organic_aod", archive[1], lat, lon)


def test_gfas_point_integrates_native_cell_area_and_actual_interval(archive):
    directory, identifier = archive
    with rasterio.open(directory / (identifier + ".tif"), "r+") as dst:
        dst.transform = from_origin(110, 0.2, 0.1, 0.1)
        dst.write(np.array([[1e-6, 0], [np.nan, 2e-6]], dtype="float32"), 1)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    layer = manifest["layers"].pop("organic_aod")
    layer["frames"][0]["interval_end"] = "2026-10-03T12:00:00Z"
    layer["display_unit"] = "t C/km²/day"
    manifest["layers"]["burnt_carbon"] = layer
    manifest_path.write_text(json.dumps(manifest))
    point = service.point("test-fire", "burnt_carbon", identifier, 0.18, 110.08)
    assert point["center"] == pytest.approx({"lat": 0.15, "lon": 110.05})
    area, _ = service.cams_emissions_service.WGS84_GEOD.geometry_area_perimeter(box(110, 0.1, 110.1, 0.2))
    assert point["mass_tonnes"] == pytest.approx(1e-6 * abs(area) * 43200 / 1000)
    assert point["value"] == pytest.approx(86.4)
    assert point["source_unit"] == "kg m-2 s-1"


def test_nodata_is_not_reported_as_zero_mass_and_flux_is_integrated_over_area():
    areas = np.array([[100, 50, 20]], dtype=float)
    result = statistics(np.array([[1e-6, 2e-6, np.nan]]), areas, "burnt_carbon")
    assert result["integrated_mass_kg"] == pytest.approx((100e-6 + 100e-6) * 86400)
    assert result["missing_cell_count"] == 1
    assert statistics(np.full((1, 3), np.nan), areas, "burnt_carbon")["integrated_mass_kg"] is None


def test_geodesic_clipping_preserves_partial_cells():
    lat, lon = np.array([0.05, -0.05]), np.array([0.05, 0.15])
    full = cell_areas(lat, lon, box(0, -0.1, 0.2, 0.1))
    clipped = cell_areas(lat, lon, box(0, -0.1, 0.05, 0.1))
    assert clipped[:, 0].sum() == pytest.approx(full[:, 0].sum() / 2, rel=1e-5)
    assert clipped[:, 1].sum() == 0


def test_invalid_administrative_polygon_can_be_repaired():
    payload = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 1], [1, 0], [0, 0]]]}
    assert geometry(payload).is_valid


def test_gfas_netcdf_end_time_maps_to_correct_daily_interval(tmp_path):
    xr = pytest.importorskip("xarray")
    data = xr.Dataset(
        {"cfire": (("valid_time", "latitude", "longitude"), np.full((1, 2, 2), 1e-6))},
        coords={
            "valid_time": [np.datetime64("2026-10-04T00:00:00")],
            "latitude": [0.05, -0.05],
            "longitude": [110.05, 110.15],
        },
    )
    data.cfire.attrs = {"GRIB_paramId": 210092, "GRIB_stepType": "avg", "units": "kg m**-2 s**-1"}
    source = tmp_path / "source.nc"
    data.to_netcdf(source)
    regions = [{"code": "61", "name": "Test", "geometry": mapping(box(110, -0.1, 110.2, 0.1))}]
    request = {"date": "2026-10-03/2026-10-03"}
    frames = ingest_file(source, tmp_path, "burnt_carbon", regions, [110, -0.1, 110.2, 0.1], request)
    assert frames[0]["valid_at"] == "2026-10-03T00:00:00Z"
    assert frames[0]["interval_end"] == "2026-10-04T00:00:00Z"
    assert frames[0]["summary"]["integrated_mass_kg"] > 0
    data.cfire.attrs["units"] = "kg m-3"
    data.to_netcdf(source, mode="w")
    with pytest.raises(CamsRequestError) as captured:
        ingest_file(source, tmp_path, "burnt_carbon", regions, [110, -0.1, 110.2, 0.1], request)
    assert captured.value.code == "source_units_mismatch"


def test_http_tile_and_download_keep_authentication_and_private_headers(archive, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes import wildfire_events

    directory, identifier = archive
    app = FastAPI()
    app.include_router(wildfire_events.router, prefix="/api")
    app.dependency_overrides[wildfire_events.router.dependencies[0].dependency] = lambda: None
    app.dependency_overrides[wildfire_events.get_db] = lambda: object()
    app.dependency_overrides[wildfire_events.get_current_disaster_viewer] = lambda: object()
    monkeypatch.setattr(wildfire_events, "_event", lambda *_: SimpleNamespace(status="published"))
    client = TestClient(app)
    prefix = "/api/disasters/wildfires/events/test-fire/emissions"
    image = client.get(f"{prefix}/tiles/organic_aod/{identifier}/0/0/0.png")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    download = client.get(f"{prefix}/download/organic_aod/{identifier}.tif")
    assert download.status_code == 200
    assert download.content == (directory / (identifier + ".tif")).read_bytes()
    assert download.headers["content-type"] == "image/tiff"
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "private, no-store"
    point = client.get(f"{prefix}/point/organic_aod/{identifier}?lat=60&lon=-20")
    assert point.status_code == 200 and point.json()["value"] == 1
    assert point.headers["cache-control"] == "private, no-store"
    assert client.get(f"{prefix}/point/organic_aod/{identifier}?lat=91&lon=0").status_code == 422
    assert client.get(f"{prefix}/point/organic_aod/missing?lat=60&lon=0").status_code == 404
    app.dependency_overrides.pop(wildfire_events.get_current_disaster_viewer)
    assert client.get(f"{prefix}/tiles/organic_aod/{identifier}/0/0/0.png").status_code == 401
    assert client.get(f"{prefix}/point/organic_aod/{identifier}?lat=60&lon=0").status_code == 401


def test_ingest_rejects_a_kalimantan_file_for_sumatra(tmp_path):
    xr = pytest.importorskip("xarray")
    data = xr.Dataset(
        {"omaod550": (("valid_time", "latitude", "longitude"), np.full((1, 2, 2), 0.4))},
        coords={"valid_time": [np.datetime64("2026-10-06T12:00:00")], "latitude": [0.4, 0], "longitude": [110, 110.4]},
    )
    data.omaod550.attrs = {"GRIB_paramId": 210210, "GRIB_stepType": "instant", "units": "1"}
    source = tmp_path / "kalimantan.nc"
    data.to_netcdf(source)
    regions = [{"code": "14", "name": "Riau", "geometry": mapping(box(100, -1, 104, 2))}]
    with pytest.raises(CamsRequestError) as captured:
        ingest_file(source, tmp_path, "organic_aod", regions, [94.9, -6.1, 109.3, 6.1], {"date": "2026-10-06/2026-10-06"})
    assert captured.value.code == "source_coverage_mismatch"
