"""
geoai_tools.py - Whitelisted tool/function layer for the SaveGeo Assistant.

Per the "Safety Against Hallucination" requirement, the LLM never gets free
SQL/code execution or direct DB/GEE access. It can only call the functions
registered in `TOOL_REGISTRY` below, each with a fixed JSON-schema signature.
Two data sources back these tools:

  1. `tool_ctx["context"]["results"]` - the analysis results already computed
     and held client-side (AnalysisResultsBundle-shaped: carbon/vegetation/
     landcover/landcover_transition), sent fresh with every chat turn. Tools
     that read from here (`get_analysis_results`, `query_*`, `compare_periods`)
     never invent a number: if a result kind is missing, they report that
     explicitly instead of estimating.
  2. Live, whitelisted Earth Engine calls for hotspot search
     (`landcover_service.analyze_landcover_hotspots`,
     `vegetation_service.analyze_vegetation_change_hotspots`) - real spatial
     analysis, not a guess, gated on an AOI geometry actually being present
     in context.

`run_agent_control`/`geoai_with_ai` build one `tool_ctx` per request and pass
it through `_execute_geoai_tool` for every tool call the model makes in that
turn's tool-calling loop.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# Tool schemas (provider-agnostic JSON Schema - adapted per-provider
# in agentic_ai.py's _call_*_tools helpers)
# ══════════════════════════════════════════════════════════════════

GEOAI_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_current_context",
        "description": (
            "Get the AOI, active period/year, active map layer, and which analysis "
            "kinds (carbon/vegetation/landcover/landcover_transition) are currently "
            "available for this session. Call this first when unsure what the user "
            "already has open."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_analysis_results",
        "description": (
            "Read the already-computed analysis result for one kind. Returns "
            "available=false (never a fabricated number) if that analysis has not "
            "been run for the current AOI/period."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["carbon", "vegetation", "landcover", "landcover_transition", "all"],
                    "description": "Which computed result to read. 'all' returns every kind that is available.",
                }
            },
            "required": ["kind"],
        },
    },
    {
        "name": "query_vegetation_index",
        "description": (
            "Read AOI-level statistics (mean/min/max/std_dev) and the classification "
            "area breakdown for one vegetation index (NDVI, EVI, SAVI, NDWI, NBR, ...) "
            "from the already-computed vegetation result. Optionally compare the AOI "
            "mean against a threshold."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {"type": "string", "description": "Index name, e.g. NDVI, EVI, NDWI, NBR."},
                "operator": {"type": "string", "enum": ["<", "<=", ">", ">=", "=="], "description": "Optional comparison operator against the AOI mean."},
                "threshold": {"type": "number", "description": "Optional threshold value to compare the AOI mean against."},
            },
            "required": ["index"],
        },
    },
    {
        "name": "query_landcover",
        "description": "Read the class-by-class area/percentage breakdown from the already-computed land cover result.",
        "parameters": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "Optional dataset key to filter to one dataset; omit to return all computed datasets."},
            },
            "required": [],
        },
    },
    {
        "name": "query_carbon",
        "description": "Read the already-computed carbon stock estimate (total, density, dataset, model, R2/uncertainty if available).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "compare_periods",
        "description": (
            "Read an already-computed before/after comparison between two periods "
            "for a metric (currently: landcover transition matrix + net class "
            "change). Does not run a new analysis - reports not-available if the "
            "user hasn't run a change/transition analysis yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["landcover", "vegetation"]},
            },
            "required": ["metric"],
        },
    },
    {
        "name": "find_hotspots",
        "description": (
            "Run real spatial hotspot detection over the active AOI: vectorizes the "
            "pixel-level change into ranked polygons (largest first). "
            "metric='vegetation_change' finds patches where a vegetation index "
            "dropped/rose beyond a threshold between two years; "
            "metric='landcover_transition' finds patches that changed class between "
            "two years. Requires an AOI to be set. This performs a real Earth Engine "
            "computation and can take a few seconds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["vegetation_change", "landcover_transition"]},
                "from_year": {"type": "integer", "description": "Start year of the comparison period."},
                "to_year": {"type": "integer", "description": "End year of the comparison period."},
                "index": {"type": "string", "description": "Vegetation index to use (metric=vegetation_change only). Default NDVI."},
                "direction": {"type": "string", "enum": ["decline", "increase", "any"], "description": "metric=vegetation_change only. Default decline (vegetation loss)."},
                "threshold": {"type": "number", "description": "Change threshold that defines a 'hotspot' pixel. Default -0.2 for decline, 0.2 for increase."},
                "dataset": {"type": "string", "description": "Land cover dataset key (metric=landcover_transition only)."},
                "top_n": {"type": "integer", "description": "Max ranked polygons to return, default 10, max 100."},
                "min_area_ha": {"type": "number", "description": "Drop polygons smaller than this many hectares, default 1."},
            },
            "required": ["metric", "from_year", "to_year"],
        },
    },
    {
        "name": "get_hotspot_detail",
        "description": (
            "Look up full detail for one hotspot id returned by a find_hotspots call "
            "earlier in this conversation (e.g. 'h2' for the 2nd-ranked hotspot)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"hotspot_id": {"type": "string"}},
            "required": ["hotspot_id"],
        },
    },
]


# ══════════════════════════════════════════════════════════════════
# Tool implementations
# ══════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(UTC).isoformat()


def _not_available(kind: str, hint: str) -> dict[str, Any]:
    return {
        "available": False,
        "message": f"Data '{kind}' belum tersedia untuk AOI/periode ini. {hint}",
    }


def get_current_context(tool_ctx: dict) -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    results = ctx.get("results") or {}
    return {
        "aoi_present": bool(ctx.get("aoi")),
        "aoi_name": ctx.get("aoi_name"),
        "area_ha": ctx.get("area_ha"),
        "bbox": ctx.get("bbox"),
        "period": ctx.get("period"),
        "selected_year": ctx.get("selected_year"),
        "active_module": ctx.get("current_module"),
        "active_layer": ctx.get("active_layer"),
        "selected_feature_present": bool(ctx.get("selected_feature")),
        "available_results": {
            "carbon": bool(results.get("carbon")),
            "vegetation": bool(results.get("vegetation")),
            "landcover": bool(results.get("landcover")),
            "landcover_transition": bool(results.get("landcover_transition")),
        },
    }


def get_analysis_results(tool_ctx: dict, kind: str = "all") -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    results = ctx.get("results") or {}

    def _one(k: str) -> dict[str, Any]:
        val = results.get(k)
        if not val:
            return _not_available(k, f"Jalankan analisis {k} terlebih dahulu di modul terkait.")
        return {
            "available": True,
            "data": val,
            "source": {
                "source": k,
                "dataset": _dataset_label(k, val),
                "period": ctx.get("period") or ctx.get("selected_year"),
                "generated_at": None,  # client-held result, exact compute time not tracked
            },
        }

    if kind == "all":
        return {k: _one(k) for k in ("carbon", "vegetation", "landcover", "landcover_transition")}
    if kind not in ("carbon", "vegetation", "landcover", "landcover_transition"):
        return {"error": f"Unknown kind '{kind}'."}
    return _one(kind)


def _dataset_label(kind: str, val: Any) -> str | None:
    if not isinstance(val, dict):
        return None
    if kind == "carbon":
        return (val.get("model_info") or {}).get("reference_dataset")
    if kind == "landcover_transition":
        return val.get("dataset") or val.get("dataset_name")
    return None


def query_vegetation_index(tool_ctx: dict, index: str, operator: str | None = None, threshold: float | None = None) -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    veg = (ctx.get("results") or {}).get("vegetation")
    if not veg:
        return _not_available("vegetation", "Jalankan analisis vegetasi terlebih dahulu.")

    indices = veg.get("indices") or {}
    stats = indices.get(index.upper()) or indices.get(index)
    if not stats:
        return {
            "available": False,
            "message": f"Indeks '{index}' belum dihitung pada analisis vegetasi yang tersedia. Indeks yang tersedia: {list(indices.keys())}.",
        }

    result: dict[str, Any] = {
        "available": True,
        "index": index,
        "mean": stats.get("mean"),
        "min": stats.get("min"),
        "max": stats.get("max"),
        "std_dev": stats.get("std_dev"),
        "classification": (stats.get("classification") or {}).get("classes"),
        "source": {"source": "vegetation_analysis", "period": ctx.get("period") or ctx.get("selected_year"), "generated_at": None},
    }
    if operator and threshold is not None and stats.get("mean") is not None:
        mean = float(stats["mean"])
        ops: dict[str, Callable[[float, float], bool]] = {
            "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "==": lambda a, b: a == b,
        }
        cmp = ops.get(operator)
        if cmp:
            result["threshold_check"] = {
                "operator": operator, "threshold": threshold,
                "aoi_mean_satisfies": cmp(mean, threshold),
                "note": "This checks the AOI-wide mean only, not per-pixel area. Use find_hotspots for a ranked list of specific areas beyond a threshold.",
            }
    return result


def query_landcover(tool_ctx: dict, dataset: str | None = None) -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    lc = (ctx.get("results") or {}).get("landcover")
    if not lc:
        return _not_available("landcover", "Jalankan analisis tutupan lahan terlebih dahulu.")

    entries = {k: v for k, v in lc.items() if isinstance(v, dict) and "classes" in v}
    if dataset:
        entries = {k: v for k, v in entries.items() if k == dataset}
        if not entries:
            return {"available": False, "message": f"Dataset '{dataset}' tidak ada pada hasil tutupan lahan yang tersedia."}
    return {
        "available": True,
        "datasets": {
            k: {"classes": v.get("classes"), "total_area_ha": v.get("total_area_ha")}
            for k, v in entries.items()
        },
        "source": {"source": "landcover_analysis", "period": ctx.get("period") or ctx.get("selected_year"), "generated_at": None},
    }


def query_carbon(tool_ctx: dict) -> dict[str, Any]:
    """Reads the CarbonResult shape actually returned by carbon_service.analyze_carbon
    (see savegeo/frontend/src/features/carbon/types.ts CarbonResult) - carbon_estimated.
    statistics for density, area_info for totals, model_info(.cv_metrics) for the model
    used and its R2/RMSE. Every figure here is a model estimate, never a direct measurement."""
    ctx = tool_ctx.get("context") or {}
    carbon = (ctx.get("results") or {}).get("carbon")
    if not carbon:
        return _not_available("carbon", "Jalankan analisis stok karbon terlebih dahulu.")

    stats = (carbon.get("carbon_estimated") or {}).get("statistics") or {}
    area_info = carbon.get("area_info") or {}
    model_info = carbon.get("model_info") or {}
    cv_metrics = model_info.get("cv_metrics") or {}
    reference = carbon.get("carbon_reference") or {}
    return {
        "available": True,
        "value_type": "estimated",  # carbon is always a model estimate, never a direct measurement
        "mean_density_mg_ha": stats.get("mean"),
        "min_density_mg_ha": stats.get("min"),
        "max_density_mg_ha": stats.get("max"),
        "std_dev": stats.get("std_dev"),
        "total_carbon_tons": area_info.get("total_carbon_tons"),
        "carbon_dioxide_equivalent_tons": area_info.get("carbon_dioxide_equivalent_tons"),
        "calculation_area_ha": area_info.get("calculation_area_ha"),
        "target_pool": model_info.get("target_pool"),
        "reference_dataset": model_info.get("reference_dataset") or reference.get("full_name") or reference.get("name"),
        "model_name": model_info.get("model_name"),
        "model_r2": cv_metrics.get("r2_mean"),
        "model_rmse": cv_metrics.get("rmse_mean"),
        "source": {"source": "carbon_analysis", "dataset": model_info.get("reference_dataset"), "generated_at": None},
    }


def compare_periods(tool_ctx: dict, metric: str) -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    if metric == "landcover":
        trans = (ctx.get("results") or {}).get("landcover_transition")
        if not trans:
            return _not_available(
                "landcover_transition",
                "Jalankan analisis perubahan tutupan lahan (LC Change) antar dua tahun terlebih dahulu.",
            )
        return {
            "available": True,
            "from_year": trans.get("from_year") or trans.get("year_a"),
            "to_year": trans.get("to_year") or trans.get("year_b"),
            "gains": trans.get("gains"),
            "losses": trans.get("losses"),
            "matrix": trans.get("matrix"),
            "source": {"source": "landcover_transition_analysis", "generated_at": None},
        }
    return _not_available(
        "vegetation_period_comparison",
        "Belum ada perbandingan vegetasi dua-periode tersimpan untuk AOI ini. Gunakan find_hotspots(metric='vegetation_change') untuk mencari area yang berubah antar dua tahun.",
    )


def _next_hotspot_ids(tool_ctx: dict, hotspots: list[dict]) -> list[str]:
    store = tool_ctx.setdefault("last_hotspots", {})
    ids = []
    for h in hotspots:
        hid = f"h{len(store) + 1}"
        store[hid] = h
        ids.append(hid)
    return ids


def find_hotspots(
    tool_ctx: dict,
    metric: str,
    from_year: int,
    to_year: int,
    index: str = "NDVI",
    direction: str = "decline",
    threshold: float | None = None,
    dataset: str | None = None,
    top_n: int = 10,
    min_area_ha: float = 1.0,
) -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    aoi_geojson = ctx.get("aoi")
    if not aoi_geojson:
        return {
            "available": False,
            "message": "AOI belum diset. Minta user menggambar AOI di peta atau memilih wilayah terlebih dahulu sebelum mencari hotspot.",
        }

    db = tool_ctx.get("db")
    aoi_payload = {"geojson": aoi_geojson}

    try:
        if metric == "vegetation_change":
            from app.services import vegetation_service

            data = {
                "aoi": aoi_payload, "index": index, "from_year": from_year, "to_year": to_year,
                "direction": direction, "top_n": top_n, "min_area_ha": min_area_ha,
            }
            if threshold is not None:
                data["threshold"] = threshold
            raw = vegetation_service.analyze_vegetation_change_hotspots(db, data)
            hotspots = raw.get("hotspots", [])
            ids = _next_hotspot_ids(tool_ctx, [
                {**h, "metric": "vegetation_change", "index": raw.get("index"), "from_year": from_year, "to_year": to_year}
                for h in hotspots
            ])
            for h, hid in zip(hotspots, ids):
                h["id"] = hid
            return {
                "available": True, "metric": "vegetation_change", "index": raw.get("index"),
                "direction": raw.get("direction"), "from_year": from_year, "to_year": to_year,
                "hotspot_count": raw.get("hotspot_count"), "hotspots": hotspots,
                "source": {"source": "vegetation_change_hotspots", "period": f"{from_year}-{to_year}", "generated_at": _now()},
            }

        if metric == "landcover_transition":
            from app.services import landcover_service

            data = {
                "aoi": aoi_payload, "dataset": dataset or ctx.get("landcover_dataset") or "Dynamic_World",
                "from_year": from_year, "to_year": to_year, "top_n": top_n, "min_area_ha": min_area_ha,
            }
            raw = landcover_service.analyze_landcover_hotspots(data)
            hotspots = raw.get("hotspots", [])
            ids = _next_hotspot_ids(tool_ctx, [
                {**h, "metric": "landcover_transition", "dataset": raw.get("dataset"), "from_year": from_year, "to_year": to_year}
                for h in hotspots
            ])
            for h, hid in zip(hotspots, ids):
                h["id"] = hid
            return {
                "available": True, "metric": "landcover_transition", "dataset": raw.get("dataset_name"),
                "from_year": from_year, "to_year": to_year,
                "hotspot_count": raw.get("hotspot_count"), "hotspots": hotspots,
                "source": {"source": "landcover_transition_hotspots", "period": f"{from_year}-{to_year}", "generated_at": _now()},
            }

        return {"error": f"Unknown metric '{metric}'."}
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error, not fabricated data
        logger.warning("find_hotspots tool failed: %s", exc)
        return {"available": False, "error": str(exc)}


def get_hotspot_detail(tool_ctx: dict, hotspot_id: str) -> dict[str, Any]:
    store = tool_ctx.get("last_hotspots") or {}
    hotspot = store.get(hotspot_id)
    if not hotspot:
        return {
            "available": False,
            "message": (
                f"Hotspot '{hotspot_id}' tidak ditemukan dari pencarian hotspot di percakapan ini. "
                "Jika user merujuk hotspot dari pesan sebelumnya, gunakan data hotspot yang sudah ada "
                "di riwayat percakapan, atau jalankan find_hotspots lagi."
            ),
        }
    return {"available": True, "hotspot": hotspot}


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_current_context": get_current_context,
    "get_analysis_results": get_analysis_results,
    "query_vegetation_index": query_vegetation_index,
    "query_landcover": query_landcover,
    "query_carbon": query_carbon,
    "compare_periods": compare_periods,
    "find_hotspots": find_hotspots,
    "get_hotspot_detail": get_hotspot_detail,
}


def execute_geoai_tool(name: str, args: dict, tool_ctx: dict) -> dict[str, Any]:
    """Dispatch a model-issued tool call to its whitelisted implementation.

    Never executes anything not in TOOL_REGISTRY - this is the enforcement
    point for "no arbitrary code/SQL execution" (spec section 12/15).
    """
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return {"error": f"Tool '{name}' is not whitelisted."}
    try:
        return fn(tool_ctx, **(args or {}))
    except TypeError as exc:
        return {"error": f"Invalid arguments for tool '{name}': {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("geoai tool '%s' failed: %s", name, exc)
        return {"error": str(exc)}
