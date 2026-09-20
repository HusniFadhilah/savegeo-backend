"""Carbon analysis endpoints - /api/analyze/carbon*. Ported from backend/app.py."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import ee
import requests
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import require_ee
from app.core.security import get_current_admin_panel, get_current_app_viewer
from app.db.session import get_db
from app.services import carbon_service
from app.services.gee_common import AnalysisError, public_analysis_error

router = APIRouter(tags=["carbon"])
logger = logging.getLogger(__name__)
UPSTREAM_CONNECTION_ERROR = (
    "Koneksi ke layanan data eksternal terputus sebelum respons diterima. "
    "Coba jalankan ulang analisis; jika berulang, kecilkan AOI/rentang waktu "
    "atau cek koneksi ke Earth Engine/penyedia raster."
)


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
    if not (meta and meta.get("ingestion_method") == "cog_rasterio") and dataset_key not in CARBON_ARCGIS_REGISTRY and not getattr(request.app.state, "ee_initialized", False):
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi untuk dataset ini.")
    try:
        return carbon_service.get_direct_carbon_reference_layer(dataset_key, year, vis_min=min, vis_max=max)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid reference layer parameters") from exc
    except ee.EEException as exc:
        logger.warning("Earth Engine reference layer failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Layer Earth Engine sedang tidak tersedia.") from exc
    except requests.RequestException as exc:
        logger.warning("Reference layer upstream connection failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=UPSTREAM_CONNECTION_ERROR) from exc


@router.post("/analyze/carbon", dependencies=[Depends(get_current_app_viewer)])
def analyze_carbon(request: Request, data: dict = Body(...), db: Session = Depends(get_db)):
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    from app.registries.carbon_dataset_registry import get_external_carbon_meta
    reference_dataset = str(data.get("reference_dataset") or "")
    external_meta = get_external_carbon_meta(reference_dataset)
    direct_reference = bool(data.get("reference_only")) or not data.get("model_name")
    requires_ee = not (direct_reference and external_meta and external_meta.get("ingestion_method") in {"cog_rasterio", "soilgrids_rest"})
    if requires_ee and not getattr(request.app.state, "ee_initialized", False):
        raise HTTPException(status_code=503, detail="Google Earth Engine belum diinisialisasi. Cek kredensial di Admin Panel.")
    try:
        return carbon_service.analyze_carbon(db, data)
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
        logger.error("Carbon analysis failed")
        raise HTTPException(status_code=500, detail="Analisis karbon gagal. Coba lagi atau hubungi administrator.") from e


@router.post("/analyze/carbon-local", dependencies=[Depends(get_current_app_viewer)])
def analyze_carbon_local(data: dict = Body(...), db: Session = Depends(get_db)):
    """Non-GEE carbon estimation path — does NOT require Earth Engine."""
    try:
        return carbon_service.analyze_carbon_local(db, data)
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
def analyze_carbon_delta(data: dict = Body(...), db: Session = Depends(get_db)):
    if "aoi" not in data:
        raise HTTPException(status_code=400, detail="Missing required field: aoi")
    try:
        return carbon_service.analyze_carbon_delta(db, data)
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
):
    """Serve XYZ tiles for public COG carbon references (Hansen/ESA/CTrees)."""
    from app.providers.local_raster_provider import record_carbon_tile_error, render_carbon_reference_tile
    from app.registries.carbon_dataset_registry import get_external_carbon_meta

    meta = get_external_carbon_meta(dataset_key)
    if not meta or meta.get("ingestion_method") != "cog_rasterio":
        raise HTTPException(status_code=404, detail="Dataset tidak memiliki tile COG publik")
    try:
        content = render_carbon_reference_tile(dataset_key, z, x, y, year=year, vis_min=min, vis_max=max)
    except Exception as exc:  # noqa: BLE001 - return a stable tile response, log details server-side
        record_carbon_tile_error()
        logger.warning("Carbon reference tile failed for %s/%s/%s/%s: %s", dataset_key, z, x, y, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Tile referensi sedang tidak tersedia") from exc
    if content is None:
        # A transparent PNG keeps Leaflet from retrying ocean/missing tiles.
        return Response(content=b"", status_code=204, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
