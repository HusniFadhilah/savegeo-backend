from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from app.registries.map_layer_registry import get_all_layers, get_basemaps

router = APIRouter(tags=["maps"])


@router.get("/map-layers")
def map_layers(module: Optional[str] = None):
    layers = get_all_layers(module=module)
    return {"layers": layers, "count": len(layers)}


@router.get("/basemaps")
def basemaps():
    layers = get_basemaps()
    return {"layers": layers, "count": len(layers)}
