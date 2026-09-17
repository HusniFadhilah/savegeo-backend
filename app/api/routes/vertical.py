"""Vertical datum endpoints with explicit EGM2008 safety checks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import get_current_app_viewer
from app.services.vertical_datum_service import (
    convert_ellipsoidal_to_orthometric,
    egm2008_status,
)

router = APIRouter(tags=["vertical-datum"])


class ElevationConversionRequest(BaseModel):
    ellipsoidal_height_m: float = Field(..., description="h: ellipsoidal height")
    geoid_undulation_m: float = Field(..., description="N: EGM2008 geoid undulation")
    source_vertical_reference: str = Field(min_length=1, max_length=128)
    geoid_model: str = Field(default="EGM2008", min_length=1, max_length=64)


@router.get("/geoid/egm2008/status")
def get_egm2008_status(_viewer: object = Depends(get_current_app_viewer)):
    return egm2008_status()


@router.post("/elevation/convert")
def convert_elevation(request: ElevationConversionRequest, _viewer: object = Depends(get_current_app_viewer)):
    try:
        return convert_ellipsoidal_to_orthometric(**request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "VERTICAL_CONVERSION_FAILED", "message": str(exc)}) from exc
