from __future__ import annotations

import io
import zipfile

import pytest
import shapefile
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import utils
from app.core.isolated_process import run_isolated
from app.core.security import get_current_app_viewer


def _sample_archive(path):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf)
    writer.field("name", "C")
    writer.point(110.0, -7.0)
    writer.record("uji")
    writer.close()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture.shp", shp.getvalue())
        archive.writestr("fixture.shx", shx.getvalue())
        archive.writestr("fixture.dbf", dbf.getvalue())


def test_shapefile_is_parsed_in_resource_limited_worker(tmp_path):
    archive = tmp_path / "fixture.zip"
    _sample_archive(archive)

    result = run_isolated(
        "shapefile",
        {"source": str(archive)},
        timeout=30,
        memory_mib=512,
        max_output_bytes=1024 * 1024,
    )

    assert result["feature_count"] == 1
    assert result["name"] == "fixture"
    feature = result["geojson"]["features"][0]
    assert feature["geometry"]["coordinates"] == [110.0, -7.0]
    assert feature["properties"]["name"] == "uji"


def test_shapefile_worker_rejects_zip_slip_members(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../fixture.shp", b"not a shapefile")

    with pytest.raises(ValueError, match="Native processing failed"):
        run_isolated("shapefile", {"source": str(archive)}, timeout=30, memory_mib=512)


def test_convert_shp_route_uses_isolated_parser(tmp_path):
    archive = tmp_path / "fixture.zip"
    _sample_archive(archive)
    app = FastAPI()
    app.include_router(utils.router, prefix="/api")
    app.dependency_overrides[get_current_app_viewer] = lambda: object()

    with TestClient(app) as client:
        response = client.post(
            "/api/utils/convert_shp",
            files={"file": ("fixture.zip", archive.read_bytes(), "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert response.json()["feature_count"] == 1
    assert response.json()["name"] == "fixture"
