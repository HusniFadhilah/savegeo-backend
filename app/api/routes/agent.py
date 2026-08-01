"""Agentic AI endpoints - /api/agent/*. Ported from backend/app.py.

None of these three routes require Earth Engine to be initialized — the
capabilities document itself reports `ee_initialized` status to the caller.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import agent_service

router = APIRouter(prefix="/agent", tags=["agent"])
logger = logging.getLogger(__name__)


@router.get("/capabilities")
def agent_capabilities(request: Request, db: Session = Depends(get_db)):
    """Expose datasets, models, tools, and guardrails available to the agent."""
    return agent_service.build_capabilities_document(db, request)


@router.post("/analyze")
async def agent_analyze(request: Request, db: Session = Depends(get_db)):
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
    payload = await request.json()
    return agent_service.run_agent_analyze(db, request, payload)


@router.post("/control")
async def agent_control(request: Request, db: Session = Depends(get_db)):
    """
    AI Controller endpoint.
    Body: { message, page_state, image?, attachment?, session_id? }
    Returns: { intent, confidence, needs_confirmation, message, warnings, actions, session_id }
    """
    payload = await request.json()
    result, status_code, headers = agent_service.run_agent_control(db, request, payload)
    return JSONResponse(content=result, status_code=status_code, headers=headers)
