import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from PIL import Image
import rasterio

from app.api.routes import wildfire_events
from app.services import cams_archive_service as service
from scripts.sync_cams_archive import WORLD_BBOX, ingest_file


def test_global_aerosol_ingest_preserves_cells_outside_kalimantan(tmp_path, monkeypatch):
    xr = pytest.importorskip("xarray")
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(cams_archive_dir=str(tmp_path)))
    directory = tmp_path / "world"
    directory.mkdir()
    lat = np.linspace(90, -90, 451)
    lon = np.arange(900) * 0.4
    data = xr.Dataset(
        {"omaod550": (("valid_time", "latitude", "longitude"), np.full((1, 451, 900), 0.4, dtype="float32"))},
        coords={"valid_time": [np.datetime64("2026-10-06T12:00:00")], "latitude": lat, "longitude": lon},
    )
    data.omaod550.attrs = {"GRIB_paramId": 210210, "GRIB_stepType": "instant", "units": "1"}
    source = tmp_path / "source.nc"
    data.to_netcdf(source)
    frames = ingest_file(source, directory, "organic_aod", [], WORLD_BBOX, {"date": "2026-10-06/2026-10-06"})
    layer = {
        **service.LAYER_DEFINITIONS["organic_aod"], "frames": frames,
        "maximum": frames[0]["maximum_display_value"],
    }
    manifest = {
        "schema_version": 1, "event_slug": "world", "bbox": WORLD_BBOX,
        "updated_at": "2026-10-07T00:00:00Z", "layers": {"organic_aod": layer},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    public = service.public_archive("world")
    assert public["bbox"] == WORLD_BBOX
    assert "burnt_carbon" not in public["layers"]
    assert frames[0]["regions"] == []
    with rasterio.open(directory / frames[0]["filename"]) as dataset:
        assert dataset.bounds.left == pytest.approx(-180.2)
        assert dataset.bounds.right == pytest.approx(179.8)
        assert dataset.width == 900
    for latitude, longitude in [(40, -100), (-25, -60), (10, 20), (50, 10), (-7, 110), (-4, 140)]:
        point = service.point("world", "organic_aod", frames[0]["id"], latitude, longitude)
        assert point["data_available"] is True
        assert point["value"] == pytest.approx(0.4)
    image = Image.open(io.BytesIO(service.tile("world", "organic_aod", frames[0]["id"], 0, 0, 0)))
    assert (np.asarray(image)[:, :, 3] > 0).all()


def test_world_archive_point_and_tile_use_global_storage_and_keep_authentication():
    app = FastAPI()
    app.include_router(wildfire_events.router, prefix="/api")
    app.dependency_overrides[wildfire_events.router.dependencies[0].dependency] = lambda: None
    app.dependency_overrides[wildfire_events.get_db] = lambda: object()
    app.dependency_overrides[wildfire_events.get_current_disaster_viewer] = lambda: object()
    client = TestClient(app)
    prefix = "/api/disasters/wildfires/events/kalimantan-2026/emissions"
    with (
        patch.object(wildfire_events, "_event", return_value=SimpleNamespace(status="published")),
        patch.object(service, "public_archive", return_value={"bbox": WORLD_BBOX}) as public,
        patch.object(service, "point", return_value={"data_available": True, "value": 0.4}) as point,
        patch.object(service, "tile", return_value=b"png") as tile,
    ):
        response = client.get(f"{prefix}/archive?scope=world")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        public.assert_called_once_with("world")
        assert client.get(f"{prefix}/point/organic_aod/frame?scope=world&lat=40&lon=-100").status_code == 200
        point.assert_called_once_with("world", "organic_aod", "frame", 40.0, -100.0)
        assert client.get(f"{prefix}/tiles/organic_aod/frame/2/0/1.png?scope=world").status_code == 200
        tile.assert_called_once_with("world", "organic_aod", "frame", 2, 0, 1)
        public.reset_mock()
        assert client.get(f"{prefix}/archive").status_code == 200
        public.assert_called_once_with("kalimantan-2026")
        app.dependency_overrides.pop(wildfire_events.get_current_disaster_viewer)
        assert client.get(f"{prefix}/archive?scope=world").status_code == 401
        assert client.get(f"{prefix}/tiles/organic_aod/frame/2/0/1.png?scope=world").status_code == 401
