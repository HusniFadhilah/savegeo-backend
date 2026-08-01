"""Executive summary report (DOCX) - /api/reports/executive-summary. Ported from app.py."""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.reporting.executive_summary import build_executive_summary_docx

router = APIRouter(prefix="/reports", tags=["reports"])


@router.post("/executive-summary")
async def executive_summary_docx(request: Request):
    payload = await request.json()
    buf = build_executive_summary_docx(payload)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    aoi_raw = str(payload.get("aoi_name") or "report")
    aoi_slug = re.sub(r"[^a-zA-Z0-9_]", "_", aoi_raw)[:30].strip("_") or "report"
    filename = f"savegeo_executive_summary_{aoi_slug}_{ts}.docx"

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
