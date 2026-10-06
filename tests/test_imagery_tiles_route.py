from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from PIL import Image

from app.api.routes import imagery_tiles


@pytest.mark.parametrize("y", [4053, 4055])
def test_public_local_raster_returns_transparent_tile_outside_coverage(y):
    image = SimpleNamespace(
        event_id=5,
        source_kind="local_upload",
        local_file_path="/var/disaster_rasters/test.tif",
    )
    event = SimpleNamespace(status="published")
    with (
        patch.object(imagery_tiles.disaster_repo, "get_imagery", return_value=image),
        patch.object(imagery_tiles.disaster_repo, "get_event", return_value=event),
        patch.object(imagery_tiles.local_imagery_tile_service, "render_tile", return_value=None),
    ):
        response = imagery_tiles.imagery_tile(39, 13, 6339, y, viewer=object(), db=object())

    assert response.status_code == 200
    assert response.media_type == "image/png"
    pixel = Image.open(BytesIO(response.body)).convert("RGBA").getpixel((0, 0))
    assert pixel[3] == 0


def test_public_local_raster_still_hides_unpublished_imagery():
    image = SimpleNamespace(
        event_id=5,
        source_kind="local_upload",
        local_file_path="/var/disaster_rasters/test.tif",
    )
    with (
        patch.object(imagery_tiles.disaster_repo, "get_imagery", return_value=image),
        patch.object(imagery_tiles.disaster_repo, "get_event", return_value=SimpleNamespace(status="draft")),
    ):
        with pytest.raises(HTTPException) as error:
            imagery_tiles.imagery_tile(39, 13, 6339, 4053, viewer=object(), db=object())

    assert error.value.status_code == 404


def test_public_local_raster_reports_render_failures_as_server_errors():
    image = SimpleNamespace(
        event_id=5,
        source_kind="local_upload",
        local_file_path="/var/disaster_rasters/test.tif",
    )
    with (
        patch.object(imagery_tiles.disaster_repo, "get_imagery", return_value=image),
        patch.object(imagery_tiles.disaster_repo, "get_event", return_value=SimpleNamespace(status="published")),
        patch.object(imagery_tiles.local_imagery_tile_service, "render_tile", side_effect=RuntimeError("bad raster")),
    ):
        with pytest.raises(HTTPException) as error:
            imagery_tiles.imagery_tile(39, 13, 6339, 4053, viewer=object(), db=object())

    assert error.value.status_code == 500
