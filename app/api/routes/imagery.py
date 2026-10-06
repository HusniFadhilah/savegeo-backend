"""Raw satellite imagery browser - /api/imagery/providers (static catalog),
/api/imagery/scenes (list real scenes with their exact acquisition date+time)
and /api/imagery/scene-tile (RGB/SAR/gas-colormap tile for one single scene;
providers marked as mosaic-capable can also use a date-range AOI mosaic) - independent of
vegetation/landcover/carbon analysis. See
app.services.imagery_service for the full rationale.
"""
from __future__ import annotations

import logging
import json
from pathlib import Path
from uuid import uuid4

import ee
from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import require_ee
from app.core.security import get_current_app_viewer
from app.core.viewer_features import require_feature
from app.core.config import get_settings
from app.db.models.user import User
from app.services import copernicus_geosave_service
from app.services import imagery_service
from app.services import samgeo_service
from app.services import advanced_imagery_service
from app.services.copernicus_geosave_service import CopernicusAnalysisError
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["imagery"], dependencies=[Depends(require_feature("app.feature.satellite_exploration"))])
logger = logging.getLogger(__name__)


@router.get("/imagery/samgeo/status")
def imagery_samgeo_status():
    return samgeo_service.capabilities()


@router.post("/imagery/samgeo/jobs", status_code=202)
async def imagery_samgeo_start(request: Request, viewer=Depends(get_current_app_viewer)):
    try:
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            data = json.loads(str(form.get("payload") or "{}"))
            if not isinstance(upload, UploadFile):
                raise AnalysisError("File GeoTIFF wajib diisi untuk upload segmentasi", 422)
            suffix = Path(upload.filename or "").suffix.casefold()
            if suffix not in {".tif", ".tiff"}:
                raise AnalysisError("Upload segmentasi hanya menerima GeoTIFF/COG", 422)
            upload_root = get_settings().disaster_raster_path / "samgeo_uploads"
            upload_root.mkdir(parents=True, exist_ok=True)
            stored = upload_root / f"{uuid4().hex}{suffix}"
            size = 0
            with stored.open("wb") as target:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > 2 * 1024 * 1024 * 1024:
                        stored.unlink(missing_ok=True)
                        raise AnalysisError("File GeoTIFF terlalu besar (maksimum 2 GB)", 413)
                    target.write(chunk)
            data["local_file_path"] = str(stored)
            data["_uploaded_local_file"] = True
            data["cleanup_local_file"] = True
        else:
            data = await request.json()
        owner = f"user:{viewer.id}" if isinstance(viewer, User) else f"admin:{viewer.id}"
        return samgeo_service.start_job(data, owner=owner)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))


@router.get("/imagery/samgeo/jobs/{job_id}")
def imagery_samgeo_result(job_id: str, viewer=Depends(get_current_app_viewer)):
    try:
        owner = f"user:{viewer.id}" if isinstance(viewer, User) else f"admin:{viewer.id}"
        return samgeo_service.get_job(job_id, owner=owner)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))


@router.get("/imagery/providers")
def imagery_providers():
    """Static (no GEE) catalog of selectable satellite/sensor providers for the
    scene browser - separate from GET /vegetation/satellites (index-analysis
    catalog); see imagery_provider_registry.py for why."""
    return imagery_service.list_providers()


@router.get("/imagery/provider-status")
def imagery_provider_status(viewer=Depends(get_current_app_viewer)):
    """Return commercial connector status without exposing credentials."""
    return {
        key: imagery_service.commercial_provider_meta(key)
        for key in imagery_service.COMMERCIAL_PROVIDER_KEYS
    }


@router.get("/imagery/nasa-gibs/layers")
def imagery_nasa_gibs_layers():
    return {"layers": imagery_service.nasa_gibs_layers()}


@router.get("/imagery/advanced/capabilities")
def imagery_advanced_capabilities(viewer=Depends(get_current_app_viewer)):
    return advanced_imagery_service.capabilities()


@router.post("/imagery/advanced/spectral-profile", dependencies=[Depends(get_current_app_viewer)])
async def imagery_spectral_profile(request: Request):
    try:
        return await run_in_threadpool(advanced_imagery_service.spectral_profile, await request.json())
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))


@router.post("/imagery/advanced/thermal-calibration", dependencies=[Depends(get_current_app_viewer)])
async def imagery_thermal_calibration(request: Request):
    try:
        return advanced_imagery_service.thermal_calibration(await request.json())
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))


@router.post("/imagery/advanced/insar/validate", dependencies=[Depends(get_current_app_viewer)])
async def imagery_insar_validate(request: Request):
    try:
        return advanced_imagery_service.validate_insar_pair(await request.json())
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))


@router.post("/imagery/raster-toolbox", dependencies=[Depends(get_current_app_viewer)])
async def imagery_raster_toolbox(request: Request):
    try:
        return await run_in_threadpool(imagery_service.raster_toolbox, await request.json())
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))


@router.get("/imagery/toolbox-export/{filename}")
def imagery_toolbox_export(filename: str):
    from pathlib import Path
    from fastapi.responses import FileResponse
    path = Path(imagery_service.get_settings().upload_dir) / "imagery-toolbox" / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Export raster tidak ditemukan")
    return FileResponse(path, media_type="image/tiff", filename=path.name)


def _is_copernicus_request(data: dict) -> bool:
    return copernicus_geosave_service.is_copernicus_provider(data.get("satellite"))


def _requires_ee(data: dict) -> bool:
    return data.get("satellite") not in {
        "big_ctsrt",
        "openaerialmap",
        "vantor_open_data",
        "planet_open_data",
        # Licensed providers are queried through their server-side STAC
        # connectors in imagery_service; they do not use Earth Engine.
        "planet_commercial",
        "vantor_commercial",
        "iceye_commercial",
        "airbus_oneatlas",
        "blacksky_spectra",
        "stac_catalog",
    } and not _is_copernicus_request(data)


@router.post("/imagery/scenes", dependencies=[Depends(get_current_app_viewer)])
async def imagery_scenes(request: Request):
    data = await request.json()
    request_id = getattr(request.state, "request_id", "unknown")
    try:
        if _requires_ee(data):
            require_ee(request)
        return await run_in_threadpool(imagery_service.list_scenes, data, request)
    except CopernicusAnalysisError as e:
        logger.warning("Imagery scenes provider error request_id=%s status=%s provider=%s", request_id, e.status_code, data.get("satellite"))
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except AnalysisError as e:
        logger.warning("Imagery scenes analysis error request_id=%s status=%s provider=%s", request_id, e.status_code, data.get("satellite"))
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        logger.warning("Imagery scenes Earth Engine error request_id=%s provider=%s: %s", request_id, data.get("satellite"), e)
        raise HTTPException(status_code=400, detail="Earth Engine rejected the imagery parameters") from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Imagery scenes error request_id=%s provider=%s", request_id, data.get("satellite"))
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e


@router.post("/imagery/scene-tile", dependencies=[Depends(get_current_app_viewer)])
async def imagery_scene_tile(request: Request):
    data = await request.json()
    try:
        if _requires_ee(data):
            require_ee(request)
        return await run_in_threadpool(imagery_service.get_scene_tile, data, request)
    except CopernicusAnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail="Earth Engine rejected the imagery parameters") from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("Imagery scene-tile error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e


@router.post("/imagery/dem-tile", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def imagery_dem_tile(request: Request):
    data = await request.json()
    try:
        return imagery_service.get_dem_tile(data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ee.EEException as e:
        raise HTTPException(status_code=400, detail="Earth Engine rejected the imagery parameters") from e
    except Exception as e:  # noqa: BLE001
        logger.error("Imagery DEM tile error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e


@router.get("/imagery/copernicus-tiles/{provider_key}/{scene_id}/{z}/{x}/{y}.png")
def imagery_copernicus_tile(provider_key: str, scene_id: str, z: int, x: int, y: int):
    try:
        png_bytes = copernicus_geosave_service.render_tile(provider_key, scene_id, z, x, y)
    except CopernicusAnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except Exception as e:  # noqa: BLE001
        logger.error("Imagery Copernicus tile error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/maxar-open-data-tiles/{z}/{x}/{y}.png")
def imagery_maxar_open_data_tile(
    z: int,
    x: int,
    y: int,
    item_url: str,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
):
    try:
        png_bytes = imagery_service.render_maxar_open_data_tile(item_url, z, x, y, asset_key, bands, rescale)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except Exception as e:  # noqa: BLE001
        logger.error("Imagery Vantor/Maxar Open Data tile error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/commercial-tiles/{provider_key}/{z}/{x}/{y}.png", dependencies=[Depends(get_current_app_viewer)])
def imagery_commercial_tile(
    provider_key: str,
    z: int,
    x: int,
    y: int,
    scene_id: str,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
):
    try:
        if not (0 <= z <= 22 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise AnalysisError("Tile coordinates are invalid", 400)
        png_bytes = imagery_service.render_commercial_stac_tile(
            provider_key, scene_id, z, x, y, asset_key, bands, rescale
        )
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except Exception as e:  # noqa: BLE001
        logger.error("Commercial imagery tile error: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e
    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile commercial di luar cakupan raster")
    return Response(content=png_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/imagery/oneatlas-tiles/{scene_id}/{z}/{x}/{y}.png", dependencies=[Depends(get_current_app_viewer)])
def imagery_oneatlas_tile(scene_id: str, z: int, x: int, y: int):
    try:
        png_bytes = imagery_service.render_oneatlas_tile(scene_id, z, x, y)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except Exception as e:  # noqa: BLE001
        logger.error("OneAtlas WMTS proxy error: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail="Airbus OneAtlas preview failed") from e
    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile OneAtlas di luar cakupan")
    return Response(content=png_bytes, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@router.get("/imagery/stac-cog-tiles/{z}/{x}/{y}.png")
def imagery_stac_cog_tile(
    z: int,
    x: int,
    y: int,
    item_url: str,
    asset_key: str = "visual",
    bands: str | None = None,
    rescale: str | None = None,
):
    try:
        png_bytes = imagery_service.render_stac_cog_tile(item_url, z, x, y, asset_key, bands, rescale)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except Exception as e:  # noqa: BLE001
        logger.error("Imagery STAC COG tile error (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Imagery analysis failed") from e

    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile di luar cakupan raster")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/imagery/big-ctsrt-tiles/{z}/{x}/{y}.png")
def imagery_big_ctsrt_tile(z: int, x: int, y: int, service: str):
    png_bytes = imagery_service.render_big_ctsrt_tile(z, x, y, service)
    if png_bytes is None:
        raise HTTPException(status_code=404, detail="Tile BIG/CTSRT tidak tersedia")
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/imagery/stac-source")
def imagery_stac_source(item_url: str, asset_key: str = "visual"):
    try:
        href = imagery_service.get_stac_asset_download_url(item_url, asset_key)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    return RedirectResponse(href)


@router.get("/imagery/commercial-source", dependencies=[Depends(get_current_app_viewer)])
def imagery_commercial_source(provider_key: str, scene_id: str, asset_key: str = "visual"):
    try:
        stream, media_type, filename = imagery_service.open_commercial_asset_stream(provider_key, scene_id, asset_key)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
