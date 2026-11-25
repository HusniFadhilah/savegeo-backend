"""Carbon analysis endpoints - /api/analyze/carbon*. Ported from backend/app.py."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import ee
import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import AppViewer, get_current_admin_panel, get_current_app_viewer
from app.core.bounded_json import read_typed_json_object
from app.core.viewer_features import require_feature
from app.db.session import get_db
from app.services import carbon_service
from app.services.gee_common import AnalysisError, public_analysis_error
from app.schemas.carbon_analysis import CarbonAnalysisRequest

router = APIRouter(tags=["carbon"], dependencies=[Depends(require_feature("app.feature.carbon_stock"))])
logger = logging.getLogger(__name__)


@router.get("/carbon/extensions/catalog", dependencies=[Depends(get_current_app_viewer)])
def carbon_extensions_catalog():
    from app.services.carbon_extensions import DETRITUS_PRESETS, IPCC_URL

    return {"detritus_presets": [{"key": key, **value} for key, value in DETRITUS_PRESETS.items()], "source": IPCC_URL,
            "method": "IPCC 2019 Refinement Vol.4 Ch.2 Table 2.2, all vegetation types; Mg C/ha. Default Tier 1 skala iklim hutan, dengan ketidakpastian tinggi; bukan pengukuran lokal."}


@router.get("/carbon/extensions/productivity-years", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
def carbon_productivity_years():
    from app.services.carbon_extensions import productivity_years, MODIS_URL

    return {"years": productivity_years(), "source": MODIS_URL}


@router.get("/carbon/extensions/blue-carbon/catalog", dependencies=[Depends(get_current_app_viewer)])
def carbon_blue_carbon_catalog(db: Session = Depends(get_db)):
    """Curated open datasets and published admin records; edits remain admin-only."""
    from app.services.config_service import get_setting
    from app.registries.blue_carbon_open_datasets import BLUE_CARBON_OPEN_DATASETS

    records = get_setting(db, "carbon.bluecarbon_profiles", [])
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except (TypeError, ValueError):
            records = []
    if not isinstance(records, list):
        records = []
    published = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("status") == "published"
        and not record.get("is_curated")
    ]
    curated_ids = {
        str(record.get("id"))
        for record in BLUE_CARBON_OPEN_DATASETS
        if isinstance(record, dict) and record.get("id") is not None
    }
    published = [record for record in published if record.get("id") not in curated_ids]
    return {"profiles": [*BLUE_CARBON_OPEN_DATASETS, *published]}


@router.get("/carbon/green-rankings/latest")
def latest_green_rankings_snapshot(
    year: int = Query(ge=2000),
    start_month: int = Query(default=1, ge=1, le=12),
    end_month: int = Query(default=12, ge=1, le=12),
    level: str = Query(default="province", pattern="^(province|city|district|village)$"),
    province: str | None = Query(default=None, max_length=40),
    city: str | None = Query(default=None, max_length=40),
    district: str | None = Query(default=None, max_length=40),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=10, le=100),
    search: str = Query(default="", max_length=100),
    sort_by: str = Query(default="rank", max_length=40),
    sort_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    viewer: AppViewer = Depends(get_current_app_viewer),
):
    """Load the latest completed public ranking for an exact year/period/scope."""
    from app.services.green_rankings import latest_snapshot, ranking_config

    _ = viewer
    try:
        config = ranking_config(
            {"level": level, "province": province, "city": city, "district": district},
            year,
            start_month,
            end_month,
        )
        return latest_snapshot(
            config,
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.get("/carbon/green-rankings/history")
def green_rankings_history(
    year: int = Query(ge=2015),
    start_month: int = Query(default=1, ge=1, le=12),
    end_month: int = Query(default=12, ge=1, le=12),
    level: str = Query(default="province", pattern="^(province|city|district|village)$"),
    province: str | None = Query(default=None, max_length=40),
    city: str | None = Query(default=None, max_length=40),
    district: str | None = Query(default=None, max_length=40),
    viewer: object = Depends(get_current_app_viewer),
):
    """Read annual top-ten snapshots and the current cohort's historical trend."""
    from app.services.green_rankings import historical_rankings, ranking_config

    _ = viewer
    try:
        config = ranking_config(
            {"level": level, "province": province, "city": city, "district": district},
            year,
            start_month,
            end_month,
        )
        return historical_rankings(config, year, top_n=10)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


UPSTREAM_CONNECTION_ERROR = (
    "Koneksi ke layanan data eksternal terputus sebelum respons diterima. "
    "Coba jalankan ulang analisis; jika berulang, kecilkan AOI/rentang waktu "
    "atau cek koneksi ke Earth Engine/penyedia raster."
)


@router.get("/carbon/green-rankings/{run_id}")
def green_rankings_page(
    run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=10, le=100),
    search: str = Query(default="", max_length=100),
    sort_by: str = Query(default="rank", max_length=40),
    sort_dir: str = Query(default="asc", pattern="^(asc|desc)$"),
    year: int | None = Query(default=None, ge=2000),
    start_month: int | None = Query(default=None, ge=1, le=12),
    end_month: int | None = Query(default=None, ge=1, le=12),
    level: str | None = Query(default=None, pattern="^(province|city|district|village)$"),
    province: str | None = Query(default=None, max_length=40),
    city: str | None = Query(default=None, max_length=40),
    district: str | None = Query(default=None, max_length=40),
    viewer: AppViewer = Depends(get_current_app_viewer),
):
    from app.api.routes.analysis_jobs import job_owner_key
    from app.services.green_rankings import (
        publish_snapshot,
        ranking_config,
        result_page,
        snapshot_page,
    )

    try:
        owner = job_owner_key(viewer)
        filter_config = None
        if run_id.startswith("snapshot:") and all(
            value is not None for value in (year, start_month, end_month, level)
        ):
            filter_config = ranking_config(
                {
                    "level": level,
                    "province": province,
                    "city": city,
                    "district": district,
                },
                year,
                start_month,
                end_month,
            )
        result = result_page(
            run_id,
            owner,
            page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filter_config=filter_config,
        )
        if result["done"] and not result.get("snapshot_saved_at"):
            scope_key = publish_snapshot(run_id, owner)
            if scope_key:
                return snapshot_page(
                    scope_key,
                    page=page,
                    page_size=page_size,
                    search=search,
                    sort_by=sort_by,
                    sort_dir=sort_dir,
                    filter_config=filter_config,
                )
        return result
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.get("/carbon/green-rankings/{run_id}/export")
def export_green_rankings(
    run_id: str,
    year: int | None = Query(default=None, ge=2000),
    start_month: int | None = Query(default=None, ge=1, le=12),
    end_month: int | None = Query(default=None, ge=1, le=12),
    level: str | None = Query(default=None, pattern="^(province|city|district|village)$"),
    province: str | None = Query(default=None, max_length=40),
    city: str | None = Query(default=None, max_length=40),
    district: str | None = Query(default=None, max_length=40),
    viewer: AppViewer = Depends(get_current_app_viewer),
):
    from app.api.routes.analysis_jobs import job_owner_key
    from app.services.green_rankings import ranking_config
    from app.services.green_rankings import csv_rows

    try:
        filter_config = None
        if run_id.startswith("snapshot:") and all(
            value is not None for value in (year, start_month, end_month, level)
        ):
            filter_config = ranking_config(
                {
                    "level": level,
                    "province": province,
                    "city": city,
                    "district": district,
                },
                year,
                start_month,
                end_month,
            )
        rows = csv_rows(run_id, job_owner_key(viewer), filter_config=filter_config)
        return StreamingResponse(rows, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="savegeo_green_rankings.csv"'})
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc


@router.get("/carbon/reference-layers/{dataset_key}", dependencies=[Depends(get_current_app_viewer)])
def direct_carbon_reference_layer(
    dataset_key: str,
    request: Request,
    year: int = Query(default=2020),
    min: float | None = Query(default=None),
    max: float | None = Query(default=None),
):
    """Return a global carbon reference tile layer without an AOI/model."""
    from app.registries.carbon_dataset_registry import CARBON_ARCGIS_REGISTRY, get_external_carbon_meta

    meta = get_external_carbon_meta(dataset_key)
    ee_initialized = bool(getattr(request.app.state, "ee_initialized", False))
    replacement_meta = None
    if meta and meta.get("ingestion_method") == "mask_only" and meta.get("replacement_key"):
        replacement_meta = get_external_carbon_meta(meta["replacement_key"])
    direct_methods = {"cog_rasterio", "soilgrids_rest", "chloris_downloads_index"}
    direct_without_ee = bool(
        (meta and meta.get("ingestion_method") in direct_methods)
        or (replacement_meta and replacement_meta.get("ingestion_method") in direct_methods)
    )
    if not direct_without_ee and dataset_key not in CARBON_ARCGIS_REGISTRY and not ee_initialized:
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi untuk dataset ini.")
    try:
        return carbon_service.get_direct_carbon_reference_layer(
            dataset_key,
            year,
            vis_min=min,
            vis_max=max,
            use_public_chloris_tiles=dataset_key == "CHLORIS_AGB_STOCK" and not ee_initialized,
        )
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid reference layer parameters") from exc
    except ee.EEException as exc:
        logger.warning("Earth Engine reference layer failed: %s", type(exc).__name__)
        if dataset_key == "CHLORIS_AGB_STOCK":
            try:
                return carbon_service.get_direct_carbon_reference_layer(
                    dataset_key,
                    year,
                    vis_min=min,
                    vis_max=max,
                    use_public_chloris_tiles=True,
                )
            except Exception as fallback_exc:  # noqa: BLE001 - retain a stable 503 if both renderers fail
                logger.warning("Public Chloris tile fallback failed: %s", type(fallback_exc).__name__)
        raise HTTPException(status_code=503, detail="Layer Earth Engine sedang tidak tersedia.") from exc
    except requests.RequestException as exc:
        logger.warning("Reference layer upstream connection failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=UPSTREAM_CONNECTION_ERROR) from exc


@router.post("/analyze/carbon", dependencies=[Depends(get_current_app_viewer)])
async def analyze_carbon(request: Request, db: Session = Depends(get_db), viewer: AppViewer = Depends(get_current_app_viewer)):
    data = await read_typed_json_object(request, CarbonAnalysisRequest)
    from app.api.routes.analysis_jobs import job_owner_key

    data = {**data, "_request_owner": job_owner_key(viewer)}
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    from app.registries.carbon_dataset_registry import get_external_carbon_meta
    reference_dataset = str(data.get("reference_dataset") or "")
    external_meta = get_external_carbon_meta(reference_dataset)
    direct_reference = bool(data.get("reference_only")) or bool(data.get("load_only")) or not data.get("model_name")
    from app.registries.carbon_dataset_registry import CARBON_ARCGIS_REGISTRY
    local_reference = external_meta and external_meta.get("ingestion_method") in {"cog_rasterio", "soilgrids_rest"}
    local_reference = local_reference or reference_dataset in CARBON_ARCGIS_REGISTRY
    if reference_dataset == "CHLORIS_AGB_STOCK" and direct_reference:
        from app.registries.carbon_dataset_registry import get_dataset_meta

        requested_year = int(data.get("dataset_year", 2010))
        chloris_meta = get_dataset_meta(reference_dataset, requested_year)
        local_reference = (
            bool(data.get("load_only"))
            or requested_year in chloris_meta.get("licensed_available_years", [])
            or requested_year not in chloris_meta.get("available_years", [])
        )
    requires_ee = not (direct_reference and local_reference)
    if requires_ee and not getattr(request.app.state, "ee_initialized", False):
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi. Cek kredensial di Admin Panel.")
    try:
        return await run_in_threadpool(carbon_service.analyze_carbon, db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ValueError as e:
        logger.warning("Carbon analysis validation error (%s)", type(e).__name__)
        raise HTTPException(status_code=400, detail="Invalid carbon analysis parameters") from e
    except ee.EEException as e:
        logger.warning("Carbon Earth Engine error: %s", type(e).__name__)
        raise HTTPException(status_code=422, detail="Earth Engine tidak dapat memproses AOI atau parameter analisis.") from e
    except requests.RequestException as e:
        logger.warning("Carbon upstream connection error: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail=UPSTREAM_CONNECTION_ERROR) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Carbon analysis failed")
        raise HTTPException(status_code=500, detail="Analisis karbon gagal. Coba lagi atau hubungi administrator.") from e


@router.post("/analyze/carbon-local", dependencies=[Depends(get_current_app_viewer)])
async def analyze_carbon_local(request: Request, db: Session = Depends(get_db)):
    """Local/STAC inference; Granite additionally downloads its HLS source from EE."""
    data = await read_typed_json_object(request, CarbonAnalysisRequest)
    if (
        data.get("model_name") == "ibm_granite_geospatial_biomass"
        and not getattr(request.app.state, "ee_initialized", False)
    ):
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi. Cek kredensial di Admin Panel.")
    try:
        return await run_in_threadpool(carbon_service.analyze_carbon_local, db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ValueError as e:
        logger.warning("Non-GEE carbon analysis validation error (%s)", type(e).__name__)
        raise HTTPException(status_code=422, detail="Invalid carbon analysis parameters") from e
    except ee.EEException as e:
        logger.warning("Non-GEE carbon Earth Engine error: %s", type(e).__name__)
        raise HTTPException(status_code=422, detail="Earth Engine tidak dapat memproses AOI atau parameter analisis.") from e
    except requests.RequestException as e:
        logger.warning("Non-GEE carbon upstream connection error: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail=UPSTREAM_CONNECTION_ERROR) from e
    except Exception as e:  # noqa: BLE001
        logger.error("Non-GEE carbon analysis failed")
        raise HTTPException(status_code=500, detail="Analisis karbon gagal. Coba lagi atau hubungi administrator.") from e


@router.post("/analyze/carbon-delta", dependencies=[Depends(get_current_app_viewer), Depends(require_ee)])
async def analyze_carbon_delta(request: Request, db: Session = Depends(get_db)):
    data = await read_typed_json_object(request, CarbonAnalysisRequest)
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return await run_in_threadpool(carbon_service.analyze_carbon_delta, db, data)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))
    except ValueError as e:
        logger.warning("Carbon delta validation error (%s)", type(e).__name__)
        raise HTTPException(status_code=400, detail="Invalid carbon analysis parameters") from e
    except ee.EEException as e:
        logger.warning("Carbon delta Earth Engine error: %s", type(e).__name__)
        raise HTTPException(status_code=422, detail="Earth Engine tidak dapat memproses AOI atau parameter analisis.") from e
    except requests.RequestException as e:
        logger.warning("Carbon delta upstream connection error: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail=UPSTREAM_CONNECTION_ERROR) from e
    except Exception as e:  # noqa: BLE001
        logger.error("Carbon delta analysis failed")
        raise HTTPException(status_code=500, detail="Analisis karbon gagal. Coba lagi atau hubungi administrator.") from e


@router.get("/carbon/reference-tiles/{dataset_key}/{z}/{x}/{y}.png")
def carbon_reference_tile(
    dataset_key: str,
    z: int,
    x: int,
    y: int,
    year: int | None = Query(default=None),
    min: float | None = Query(default=None),
    max: float | None = Query(default=None),
    palette: str | None = Query(default=None),
):
    """Serve XYZ tiles for public COG carbon references (Hansen/ESA/CTrees)."""
    from app.providers.local_raster_provider import record_carbon_tile_error, render_carbon_reference_tile
    from app.registries.carbon_dataset_registry import get_external_carbon_meta

    meta = get_external_carbon_meta(dataset_key)
    if not meta or meta.get("ingestion_method") != "cog_rasterio":
        raise HTTPException(status_code=404, detail="Dataset tidak memiliki tile COG publik")
    parsed_palette = None
    if palette:
        colors = [color.strip().lstrip("#") for color in palette.split(",")]
        if not 2 <= len(colors) <= 12 or any(
            len(color) != 6 or any(char not in "0123456789abcdefABCDEF" for char in color)
            for color in colors
        ):
            raise HTTPException(status_code=400, detail="Palet warna tidak valid")
        parsed_palette = colors
    try:
        content = render_carbon_reference_tile(
            dataset_key, z, x, y, year=year, vis_min=min, vis_max=max,
            vis_palette=parsed_palette,
        )
    except Exception as exc:  # noqa: BLE001 - return a stable tile response, log details server-side
        record_carbon_tile_error()
        logger.warning("Carbon reference tile failed for %s/%s/%s/%s: %s", dataset_key, z, x, y, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Tile referensi sedang tidak tersedia") from exc
    if content is None:
        # A transparent PNG keeps Leaflet from retrying ocean/missing tiles.
        return Response(content=b"", status_code=204, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/carbon/chloris/reference-tiles/{z}/{x}/{y}.png", dependencies=[Depends(get_current_app_viewer)])
def chloris_reference_tile(
    z: int,
    x: int,
    y: int,
    year: int = Query(..., ge=2000, le=2100),
    min: float | None = Query(default=None),
    max: float | None = Query(default=None),
):
    """Render licensed Chloris GeoTIFFs with credentials kept on the backend."""
    from app.providers.local_raster_provider import render_carbon_reference_tile
    from app.services.chloris_service import ChlorisUnavailableError

    try:
        content = render_carbon_reference_tile("CHLORIS_AGB_STOCK", z, x, y, year, min, max)
    except ChlorisUnavailableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Licensed Chloris tile failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Tile Chloris berlisensi belum dapat diakses.") from exc
    return Response(
        content=content or b"", status_code=200 if content else 204,
        media_type="image/png", headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/carbon/datasets/health")
def carbon_dataset_health():
    """Return cached reachability checks for public external carbon sources."""
    return _carbon_dataset_health(refresh=False)


@router.post("/carbon/datasets/health/refresh", dependencies=[Depends(get_current_admin_panel)])
def refresh_carbon_dataset_health():
    """Allow an admin to force upstream health checks."""
    return _carbon_dataset_health(refresh=True)


def _carbon_dataset_health(refresh: bool):
    from app.registries.carbon_dataset_registry import CARBON_EXTERNAL_REGISTRY
    from app.providers.local_raster_provider import get_carbon_tile_metrics

    keys = list(CARBON_EXTERNAL_REGISTRY)
    if refresh:
        with ThreadPoolExecutor(max_workers=min(4, len(keys) or 1)) as executor:
            datasets = list(executor.map(lambda key: carbon_service.check_external_dataset_health(key, refresh=True), keys))
    else:
        datasets = [carbon_service.get_cached_external_dataset_health(key) for key in keys]

    return {
        "datasets": datasets,
        "tile_cache": get_carbon_tile_metrics(),
        "checked_at": datetime.now(UTC).isoformat(),
    }


@router.get("/carbon/tiles/metrics")
def carbon_tile_metrics():
    """Return process-local COG tile counters for dashboards and probes."""
    from app.providers.local_raster_provider import get_carbon_tile_metrics

    return {
        "tile_cache": get_carbon_tile_metrics(),
        "checked_at": datetime.now(UTC).isoformat(),
    }
