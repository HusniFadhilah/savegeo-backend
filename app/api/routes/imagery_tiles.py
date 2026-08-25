"""XYZ tile endpoint for `SatelliteImagery` rows with
`source_kind="local_upload"` - the tile-serving side of
app.services.local_imagery_tile_service. Renders a local Cloud-Optimized
GeoTIFF on demand; no Earth Engine/GCS involved (built for BlackSky/BSG
disaster imagery whose GCP billing account is closed - see
savegeo/backend/docs/ntt-earthquake-integration-prompt.md).

Requires `get_current_disaster_viewer` + the parent event's `status == "published"` -
same publish boundary every other `/disasters/...` User route enforces (see
`disaster_events.py`'s `_get_published_event`): a `source_kind="local_upload"`
tile must not be fetchable by imagery_id alone while its event is still a
draft, or that leaks that a draft event exists. Admin gets its own preview
route (`GET /admin/disasters/imagery-tiles/...` in admin_disaster.py) for
checking imagery before publish.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.security import get_current_disaster_viewer
from app.db.session import get_db
from app.repositories import disaster_repo
from app.services import local_imagery_tile_service

router = APIRouter(tags=["imagery-tiles"])
logger = logging.getLogger(__name__)

_CACHE_CONTROL = "public, max-age=86400"


@router.get("/disasters/imagery-tiles/{imagery_id}/{z}/{x}/{y}.png")
def imagery_tile(
    imagery_id: int,
    z: int,
    x: int,
    y: int,
    viewer=Depends(get_current_disaster_viewer),
    db: Session = Depends(get_db),
):
    img = disaster_repo.get_imagery(db, imagery_id)
    if img is None or img.source_kind != "local_upload" or not img.local_file_path:
        raise HTTPException(status_code=404, detail=f"No local raster registered for imagery {imagery_id}")
    event = disaster_repo.get_event(db, img.event_id)
    if event is None or event.status != "published":
        raise HTTPException(status_code=404, detail=f"No local raster registered for imagery {imagery_id}")

    try:
        png_bytes = local_imagery_tile_service.render_tile(img.local_file_path, z, x, y)
    except Exception as e:  # noqa: BLE001
        logger.error(f"imagery_tile error (imagery_id={imagery_id} {z}/{x}/{y}): {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(content=png_bytes, media_type="image/png", headers={"Cache-Control": _CACHE_CONTROL})
