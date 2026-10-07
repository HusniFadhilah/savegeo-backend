"""Application command contracts. Dataset/provider choices always come from runtime catalogs."""
from __future__ import annotations

import re
from typing import Any

_ACTIONS = [
    ("aoi", "select_aoi", "aoi.active", None, {"query": {"type": "string", "minLength": 3, "maxLength": 200}}, ["query"], ["valid polygon", "map"]),
    ("scenes", "search_scenes", "scenes.searchRequest", "app.feature.satellite_exploration", {}, [], ["aoi", "configured provider", "date range"]),
    ("scene", "show_scene", "scenes.scene", "app.feature.satellite_exploration", {"scene_id": {"type": "string"}, "visible": {"type": "boolean"}}, ["scene_id", "visible"], ["real scene search results"]),
    ("carbon", "run_analysis", "carbon.analysis", "app.feature.carbon_stock", {}, [], ["aoi", "dataset", "year", "compatible model or reference mode"]),
    ("landcover", "run_analysis", "carbon.analysis", "app.feature.land_cover", {}, [], ["aoi", "dataset", "year", "analysisType=landcover"]),
    ("vegetation", "run_analysis", "carbon.analysis", "app.feature.vegetation_indices", {}, [], ["aoi", "sensor", "year", "indices", "analysisType=vegetation"]),
    ("change", "run_analysis", "lc_change.analysis", "app.feature.land_cover_change", {}, [], ["aoi", "dataset", "fromYear", "toYear", "data for both periods"]),
    ("field", "select_field", "crop.field", "app.feature.crop_monitoring", {"field_id": {"type": "integer", "minimum": 1}}, ["field_id"], ["saved field catalog"]),
    ("crop", "run_analysis", "crop.analysis", "app.feature.crop_monitoring", {}, [], ["selected field with commodity", "monitoring period"]),
    ("dem", "run_analysis", "disaster.dem", "app.feature.disaster_mapping", {}, [], ["aoi"]),
    ("disaster", "run_analysis", "disaster.event", "app.feature.disaster_mapping", {
        "event_type": {"type": "string", "enum": ["flood", "fire", "landslide"]},
        **{k: {"type": "string", "format": "date"} for k in ("before_start", "before_end", "after_start", "after_end")}
    }, ["event_type", "before_start", "before_end", "after_start", "after_end"], ["aoi", "nonoverlapping before/after periods", "real satellite data"]),
]


def application_action_catalog(allowed_features: set[str] | None = None) -> list[dict[str, Any]]:
    return [
        {"id": key, "action": action, "target": target,
         "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
         "prerequisites": prerequisites, "status": "supported", "data_availability": "must be checked by backend for AOI/period",
         "result": "owning React module state, result panel and map layers",
         "requires_confirmation": action == "run_analysis"}
        for key, action, target, feature, properties, required, prerequisites in _ACTIONS
        if feature is None or allowed_features is None or feature in allowed_features
    ]


def classify_application_intent(message: str) -> str | None:
    text = message.casefold()
    if re.search(r"analisis apa|analys[ie]s.*available|yang relevan", text):
        return "capabilities"
    for intent, pattern in [
        ("scenes", r"scene|citra.*(?:terbaru|satelit)|sentinel.*terbaru"),
        ("change", r"change detection|perubahan tutupan|bandingkan.*tutupan|transisi"),
        ("crop", r"pemantauan tanaman|crop monitoring|monitor.*tanaman"),
        ("disaster", r"banjir|flood|longsor|landslide|kebakaran|bencana"),
        ("carbon", r"stok karbon|carbon stock|estimasi karbon"),
        ("landcover", r"tutupan lahan|land.?cover|klasifikasi"),
        ("vegetation", r"vegetasi|ndvi|evi"),
        ("navigation", r"(?:tampilkan|pilih|zoom|buka).*?(?:aoi|wilayah)|^aoi "),
    ]:
        if re.search(pattern, text):
            return intent
    return None


APPLICATION_CONTROL_PROMPT = """
Application actions extend the existing UI command whitelist:
- select_aoi / aoi.active, parameters {"query":"Kota Semarang"}. Use this FIRST when a place is named.
  It resolves actual polygons via the existing geocode search, asks the user to choose ambiguous candidates,
  writes the shared AOI store, highlights the boundary and fits the active map. Never invent coordinates.
  A select_aoi followed by search/analysis in the same queue is supported; failure stops the queue.
- select_field / crop.field, parameters {"field_id":17}. Only use IDs in ui.crop.availableFieldIds;
  field selection also selects its AOI. Ask the user to choose a saved field/commodity if ambiguous.
- show_scene / scenes.scene may use {"scene_id":"latest","visible":true} AFTER search_scenes.
  The frontend resolves latest by actual acquisition time in the returned results. It is latest within
  the chosen date/cloud filters; mention truncation and never imply global catalog completeness.
- run_analysis / disaster.event requires event_type (flood/fire/landslide), before_start, before_end,
  after_start, after_end (ISO dates). Require before_start < before_end <= after_start < after_end.
  Ask for missing event dates; DEM is terrain context and cannot substitute for a flood impact map.
  Real /disaster/event-map handles coverage failures and the UI displays its raster, legend and metadata.
Use context.action_catalog for available actions and prerequisites. context.runtime reports backend initialization; an uninitialized Earth Engine cannot run GEE workflows. Do not promise availability when runtime/data is missing. Provider/dataset/model choices
must come from actual runtime catalogs. Open the owning module before changing parameters.
Navigation alone never starts analysis. Execution requests with all prerequisites get a concrete
queue; costly run_analysis queues need confirmation. Do not claim execution success in a plan.
For available/relevant analyses, list supported actions separately from verified AOI data coverage,
run requested fully specified workflows, and ask only missing mandatory inputs. Never assume data
coverage just because an endpoint exists. Job IDs and statuses in context.jobs are backend reports;
do not invent percentages or cancellation support. Retain AOI, dates and field from current context.
"""
