"""Executive summary report (DOCX) - /api/reports/executive-summary. Ported from app.py."""
from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.reporting.executive_summary import build_executive_summary_docx
from app.core.security import get_current_app_viewer
from app.core.viewer_features import ensure_feature_access
from app.db.models.admin_user import AdminUser
from app.db.models.user import User
from app.db.session import get_db
from sqlalchemy.orm import Session

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("/executive-summary")
async def executive_summary_docx(
    request: Request,
    viewer: AdminUser | User = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    payload = await request.json()
    result = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    included = {
        "carbon": "app.feature.carbon_stock",
        "landcover": "app.feature.land_cover",
        "vegetation": "app.feature.vegetation_indices",
    }
    requested = [code for key, code in included.items() if result.get(key)]
    for code in requested or ["app.feature.carbon_stock"]:
        ensure_feature_access(viewer, code, db)
    buf = build_executive_summary_docx(payload)

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    aoi_raw = str(payload.get("aoi_name") or "report")
    aoi_slug = re.sub(r"[^a-zA-Z0-9_]", "_", aoi_raw)[:30].strip("_") or "report"
    filename = f"savegeo_executive_summary_{aoi_slug}_{ts}.docx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
