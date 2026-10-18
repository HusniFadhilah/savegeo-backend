"""Agentic AI endpoints - /api/agent/*. Ported from backend/app.py.

None of these three routes require Earth Engine to be initialized — the
capabilities document itself reports `ee_initialized` status to the caller.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.agentic import agentic_ai
from app.core.chat_ownership import chat_session_owner_key
from app.core.security import get_current_app_viewer
from app.core.viewer_features import viewer_feature_codes
from app.db.session import get_db
from app.services import agent_service
from app.services import ai_provider_service

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


@router.get("/capabilities")
def agent_capabilities(
    request: Request,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """Expose datasets, models, tools, and guardrails available to the agent."""
    return agent_service.build_capabilities_document(db, request, viewer_feature_codes(db, viewer))


@router.post("/analyze")
async def agent_analyze(
    request: Request,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """
    Agentic AI entrypoint.

    Body:
      {
        "message": "Analisis karbon AOI ini pakai GEDI",
        "aoi": {...},
        "execute": false
      }

    By default the agent returns a validated plan. Set execute=true to run the
    whitelisted GeoMoka analysis endpoint selected by the planner.
    """
    payload = await _json_object(request)
    return await run_in_threadpool(
        agent_service.run_agent_analyze, db, request, payload, viewer_feature_codes(db, viewer)
    )


@router.post("/control")
async def agent_control(
    request: Request,
    viewer: object = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    """
    AI Controller endpoint.
    Body: { message, page_state, image?, attachment?, session_id? }
    Returns: { intent, confidence, needs_confirmation, message, warnings, actions, session_id }
    """
    payload = await _json_object(request)
    # The model call blocks for seconds; keep it off the event loop.
    result, status_code, headers = await run_in_threadpool(
        agent_service.run_agent_control,
        db,
        request,
        payload,
        owner_key=chat_session_owner_key(viewer),
        allowed_features=viewer_feature_codes(db, viewer),
    )
    return JSONResponse(content=result, status_code=status_code, headers=headers)


@router.get("/providers")
async def agent_providers(viewer: object = Depends(get_current_app_viewer)):
    """Provider/model choices the signed-in user may select, with live status.

    No base URLs, keys or raw errors are returned.
    """
    cfg = await run_in_threadpool(agentic_ai.load_ai_config)
    return await run_in_threadpool(ai_provider_service.public_catalog, cfg)
