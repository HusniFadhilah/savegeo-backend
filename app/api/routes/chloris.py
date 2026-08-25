"""Chloris carbon stock integration diagnostics."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.chloris_service import chloris_config_status, resolve_chloris_download

router = APIRouter(prefix="/carbon/chloris", tags=["chloris"])


@router.get("/status")
def chloris_status(
    product: str = Query("stock", description="Chloris download product, e.g. stock or change"),
    year: int | None = Query(None, description="Optional target year"),
):
    status = chloris_config_status()
    resolved = None
    error = None
    try:
        download = resolve_chloris_download(product=product, year=year)
        resolved = {
            "product": download.product,
            "date": download.date,
            "format": download.format,
            "url": download.url,
            "source": download.metadata.get("source") or "downloads.json",
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics endpoint should report setup issues
        error = str(exc)

    return {
        "configured": any(
            status[key]
            for key in (
                "has_data_path",
                "has_organization_id",
                "has_reporting_unit_id",
                "has_id_token",
            )
        ),
        "status": status,
        "resolved_download": resolved,
        "error": error,
    }
