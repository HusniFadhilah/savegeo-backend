"""Chloris carbon stock integration diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_app_viewer
from app.core.viewer_features import require_feature
from app.services.chloris_service import chloris_availability, chloris_config_status, resolve_chloris_download

router = APIRouter(
    prefix="/carbon/chloris",
    tags=["chloris"],
    dependencies=[Depends(require_feature("app.feature.carbon_stock"))],
)


@router.get("/status", dependencies=[Depends(get_current_app_viewer)])
def chloris_status(
    product: str = Query("stock", description="Chloris download product, e.g. stock or change"),
    year: int | None = Query(None, description="Optional target year"),
):
    status = chloris_config_status()
    availability = chloris_availability(product)
    resolved = None
    error = None
    try:
        download = resolve_chloris_download(product=product, year=year or availability["latest_available_year"])
        resolved = {
            "product": download.product,
            "date": download.date,
            "format": download.format,
            "source": download.metadata.get("source") or "downloads.json",
            "year": download.metadata.get("year"),
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics endpoint should report setup issues
        error = str(exc)

    return {
        "configured": bool(availability["available_years"]),
        "status": status,
        **availability,
        "resolved_download": resolved,
        "error": error,
    }
