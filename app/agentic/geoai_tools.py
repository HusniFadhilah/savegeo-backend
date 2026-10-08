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
                    "kinds (carbon/vegetation/landcover/landcover_transition/crop/disaster/direct) are currently "
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
                    "enum": ["carbon", "vegetation", "landcover", "landcover_transition", "crop", "disaster", "direct", "all"],
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


DIRECT_REFERENCE_FEATURES = {
    "carbon": "app.feature.carbon_stock",
    "landcover": "app.feature.land_cover",
}
DIRECT_REFERENCE_CONTEXT_FIELDS = frozenset({
    "analysis_kind",
    "dataset",
    "dataset_name",
    "year",
    "requested_year",
    "effective_year",
    "available_years",
    "resolution",
    "unit",
    "target_pool",
    "provider_type",
    "date_range",
    "direct",
    "statistics_available",
    "load_note",
})


def filter_direct_reference_layers(layers: Any, allowed_features: set[str]) -> list[dict[str, Any]]:
    """Return only attributed reference layers allowed for this viewer.

    Unknown/legacy layer records fail closed because the old combined result
    shape did not say whether a row came from carbon or land-cover analysis.
    Internal tile URLs and visualization parameters are not needed by the LLM.
    """
    if not isinstance(layers, list):
        return []
    visible: list[dict[str, Any]] = []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        feature = DIRECT_REFERENCE_FEATURES.get(layer.get("analysis_kind"))
        if feature is None or feature not in allowed_features:
            continue
        visible.append({key: value for key, value in layer.items() if key in DIRECT_REFERENCE_CONTEXT_FIELDS})
    return visible


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
    page_state = tool_ctx.get("page_state") or {}
    ui = page_state.get("ui") or {}
    scenes = ui.get("scenes") or {}
    results = ctx.get("results") or {}
    available_results = {
        "carbon": bool(results.get("carbon")),
        "vegetation": bool(results.get("vegetation")),
        "landcover": bool(results.get("landcover")),
        "landcover_transition": bool(results.get("landcover_transition")),
        "crop": bool(results.get("crop")),
        "disaster": bool(results.get("disaster")),
    }
    allowed = tool_ctx.get("allowed_features")
    if allowed is not None:
        result_features = {
            "carbon": "app.feature.carbon_stock",
            "direct": "app.feature.carbon_stock",
            "vegetation": "app.feature.vegetation_indices",
            "landcover": "app.feature.land_cover",
            "landcover_transition": "app.feature.land_cover_change",
            "crop": "app.feature.crop_monitoring",
            "disaster": "app.feature.disaster_mapping",
        }
        available_results = {
            name: present for name, present in available_results.items()
            if result_features[name] in allowed
        }
        if filter_direct_reference_layers(results.get("direct"), allowed):
            available_results["direct"] = True
    else:
        available_results["direct"] = bool(results.get("direct"))
    scene_search = {
        "provider": scenes.get("provider"),
        "scene_count": scenes.get("sceneCount"),
        "truncated": scenes.get("truncated"),
        "page": scenes.get("page"),
        "page_count": scenes.get("pageCount"),
        "selected_scene_id": scenes.get("selectedSceneId"),
        "scene_visible": scenes.get("sceneVisible"),
        "scenes": scenes.get("scenes", []),
        "providers": scenes.get("providers", []),
    }
    if allowed is not None and "app.feature.satellite_exploration" not in allowed:
        scene_search = {}
    return {
        "action_catalog": ctx.get("action_catalog", []),
        "runtime": ctx.get("runtime", {}),
        "coverage_status": "Not verified by the action/provider catalog; only actual analysis results establish coverage.",
        "jobs": ctx.get("jobs", []),
        "aoi_present": bool(ctx.get("aoi")),
        "aoi_name": ctx.get("aoi_name"),
        "area_ha": ctx.get("area_ha"),
        "bbox": ctx.get("bbox"),
        "period": ctx.get("period"),
        "selected_year": ctx.get("selected_year"),
        "active_module": ctx.get("current_module"),
        "active_layer": ctx.get("active_layer"),
        "route": page_state.get("route"),
        "scene_search": scene_search,
        "selected_feature_present": bool(ctx.get("selected_feature")),
        "available_results": available_results,
    }


def get_analysis_results(tool_ctx: dict, kind: str = "all") -> dict[str, Any]:
    ctx = tool_ctx.get("context") or {}
    results = ctx.get("results") or {}

    def _one(k: str) -> dict[str, Any]:
        val = results.get(k)
        if not val:
            return _not_available(k, f"Jalankan analisis {k} terlebih dahulu di modul terkait.")
        reference_only = k == "carbon" and isinstance(val, dict) and (
            (val.get("carbon_estimated") or {}).get("inference_mode") == "reference_only"
        )
        period = (
            (val.get("carbon_reference") or {}).get("year") or
            (val.get("model_info") or {}).get("reference_dataset_year")
        ) if reference_only else ctx.get("period") or ctx.get("selected_year")
        if k == "direct" and isinstance(val, list):
            period = _direct_reference_period(val)
        return {
            "available": True,
            "data": val,
            "source": {
                "source": "carbon_reference" if reference_only else "direct_reference" if k == "direct" else k,
                "dataset": _dataset_label(k, val),
                "period": period,
                "generated_at": None,  # client-held result, exact compute time not tracked
            },
        }

    if kind == "all":
        return {k: _one(k) for k in ("carbon", "vegetation", "landcover", "landcover_transition", "crop", "disaster", "direct")}
    if kind not in ("carbon", "vegetation", "landcover", "landcover_transition", "crop", "disaster", "direct"):
        return {"error": f"Unknown kind '{kind}'."}
    return _one(kind)


def _dataset_label(kind: str, val: Any) -> str | None:
    if kind == "direct" and isinstance(val, list):
        return ", ".join(
            item.get("dataset_name") or item.get("dataset") or ""
            for item in val if isinstance(item, dict)
        ) or None
    if not isinstance(val, dict):
        return None
    if kind == "carbon":
        reference = val.get("carbon_reference") or {}
        return reference.get("full_name") or reference.get("name") or (val.get("model_info") or {}).get("reference_dataset")
    if kind == "landcover_transition":
        return val.get("dataset") or val.get("dataset_name")
    return None


def _direct_reference_period(layers: list[dict[str, Any]]) -> int | str | None:
    years = [
        layer.get("effective_year") or layer.get("year")
        for layer in layers
        if isinstance(layer, dict) and (layer.get("effective_year") or layer.get("year")) is not None
    ]
    distinct = {str(year) for year in years}
    return years[0] if len(distinct) == 1 else None


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
    reference_only = (carbon.get("carbon_estimated") or {}).get("inference_mode") == "reference_only" or model_info.get("display_mode") == "reference_only"
    dataset_label = reference.get("full_name") or reference.get("name") or model_info.get("reference_dataset")
    return {
        "available": True,
        "value_type": "reference_dataset" if reference_only else "model_estimate",
        "mean_density_mg_ha": stats.get("mean"),
        "min_density_mg_ha": stats.get("min"),
        "max_density_mg_ha": stats.get("max"),
        "std_dev": stats.get("std_dev"),
        "total_carbon_tons": area_info.get("total_carbon_tons"),
        "carbon_dioxide_equivalent_tons": area_info.get("carbon_dioxide_equivalent_tons"),
        "calculation_area_ha": area_info.get("calculation_area_ha"),
        "target_pool": model_info.get("target_pool") or reference.get("target_pool"),
        "reference_dataset": dataset_label,
        "reference_dataset_year": model_info.get("reference_dataset_year") or reference.get("year"),
        "reference_provider": reference.get("provider_type"),
        "reference_resolution": reference.get("resolution"),
        "model_name": None if reference_only else model_info.get("model_name"),
        "model_r2": None if reference_only else cv_metrics.get("r2_mean"),
        "model_rmse": None if reference_only else cv_metrics.get("rmse_mean"),
        "source": {"source": "carbon_reference" if reference_only else "carbon_analysis", "dataset": dataset_label,
                   "period": model_info.get("reference_dataset_year") or reference.get("year") if reference_only else model_info.get("analysis_year"),
                   "generated_at": None},
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
    allowed = tool_ctx.get("allowed_features")
    if allowed is not None:
        required: str | None = {
            "query_vegetation_index": "app.feature.vegetation_indices",
            "query_landcover": "app.feature.land_cover",
            "query_carbon": "app.feature.carbon_stock",
        }.get(name)
        if name == "find_hotspots":
            metric = str((args or {}).get("metric") or "")
            required = (
                "app.feature.vegetation_indices" if metric == "vegetation_change"
                else "app.feature.land_cover_change" if metric == "landcover_transition"
                else "invalid"
            )
        if name == "compare_periods":
            metric = str((args or {}).get("metric") or "")
            required = (
                "app.feature.vegetation_indices" if metric == "vegetation"
                else "app.feature.land_cover_change" if metric == "landcover"
                else "invalid"
            )
        if name == "get_analysis_results":
            kind = str((args or {}).get("kind") or "all")
            kind_features = {
                "carbon": "app.feature.carbon_stock",
                "vegetation": "app.feature.vegetation_indices",
                "landcover": "app.feature.land_cover",
                "landcover_transition": "app.feature.land_cover_change",
                "crop": "app.feature.crop_monitoring",
                "disaster": "app.feature.disaster_mapping",
            }
            if kind == "all":
                safe_args = dict(args or {})
                result = fn(tool_ctx, **safe_args)
                safe_results = {
                    key: value for key, value in result.items()
                    if key in kind_features and kind_features[key] in allowed
                }
                direct_result = result.get("direct")
                direct_layers = filter_direct_reference_layers(
                    direct_result.get("data") if isinstance(direct_result, dict) else None,
                    allowed,
                )
                if direct_layers and isinstance(direct_result, dict):
                    safe_direct = {**direct_result, "data": direct_layers}
                    safe_direct["source"] = {
                        **(direct_result.get("source") or {}),
                        "dataset": _dataset_label("direct", direct_layers),
                        "period": _direct_reference_period(direct_layers),
                    }
                    safe_results["direct"] = safe_direct
                return safe_results
            if kind == "direct":
                direct_result = fn(tool_ctx, **(args or {}))
                direct_layers = filter_direct_reference_layers(
                    direct_result.get("data") if isinstance(direct_result, dict) else None,
                    allowed,
                )
                if not direct_layers:
                    return {"error": "Akses ke data fitur ini tidak diizinkan."}
                safe_direct = {**direct_result, "data": direct_layers}
                safe_direct["source"] = {
                    **(direct_result.get("source") or {}),
                    "dataset": _dataset_label("direct", direct_layers),
                    "period": _direct_reference_period(direct_layers),
                }
                return safe_direct
            required = kind_features.get(kind, "invalid")
        if required and required not in allowed:
            return {"error": "Akses ke data fitur ini tidak diizinkan."}
    try:
        return fn(tool_ctx, **(args or {}))
    except TypeError as exc:
        return {"error": f"Invalid arguments for tool '{name}': {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("geoai tool '%s' failed: %s", name, exc)
        return {"error": str(exc)}
