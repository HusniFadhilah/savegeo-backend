"""Application command contracts. Dataset/provider choices always come from runtime catalogs."""
from __future__ import annotations

import re
from typing import Any

_ACTIONS = [
    ("aoi", "select_aoi", "aoi.active", None, {"query": {"type": "string", "minLength": 3, "maxLength": 200}}, ["query"], ["valid polygon", "map"]),
    ("aoi_active", "use_active_aoi", "aoi.active", None, {}, [], ["an AOI is already selected"]),
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


# These are the same state keys handled by the owning React modules. Keep the
# planner catalog and backend target whitelist derived from this one contract.
_PARAMETER_SPECS = {
    "carbon.analysisYear": {"type": "integer", "minimum": 1900, "maximum": 2100},
    "carbon.referenceDataset": {"type": "string", "minLength": 1},
    "carbon.datasetYear": {"type": "integer", "minimum": 1900, "maximum": 2100},
    "carbon.modelName": {"type": ["string", "null"]},
    **{f"carbon.{key}": {"type": "boolean"} for key in (
        "referenceOnly", "loadOnly", "directGlobal", "showReference", "includeImprobableClasses"
    )},
    "carbon.clipMode": {"type": "string", "enum": ["clipped", "full"]},
    **{f"{namespace}.{key}": {"type": "integer", "minimum": 1, "maximum": 12}
       for namespace in ("carbon", "lc_change") for key in ("startMonth", "endMonth")},
    "carbon.cloudThreshold": {"type": "integer", "minimum": 0, "maximum": 100},
    **{f"carbon.{key}": {"type": "array", "items": {"type": "string"}, "minItems": 1}
       for key in ("vegetationIndices", "landcoverDatasets")},
    "carbon.vegetationSensor": {"type": "string", "minLength": 1},
    "carbon.vegetationStartMonth": {"type": "integer", "minimum": 1, "maximum": 12},
    "carbon.vegetationEndMonth": {"type": "integer", "minimum": 1, "maximum": 12},
    "carbon.vegetationCloudThreshold": {"type": "integer", "minimum": 0, "maximum": 100},
    "lc_change.dataset": {"type": "string", "minLength": 1},
    **{f"lc_change.{key}": {"type": "integer", "minimum": 1900, "maximum": 2100}
       for key in ("fromYear", "toYear")},
    "scenes.cloudMax": {"type": "integer", "minimum": 0, "maximum": 100},
    "scenes.search": {"type": "string", "maxLength": 200},
    "scenes.page": {"type": "integer", "minimum": 1},
    "scenes.pageSize": {"type": "integer", "enum": [25, 50, 100]},
    "crop.index": {"type": "string", "minLength": 1},
    "crop.periodMode": {"type": "string", "enum": ["current_season", "30d", "90d", "custom"]},
}


SHARED_CARBON_PARAMETERS = {
    key: {"app.feature.carbon_stock", "app.feature.land_cover", "app.feature.vegetation_indices"}
    for key in ("carbon.analysisYear", "carbon.startMonth", "carbon.endMonth")
}
SHARED_CARBON_PARAMETERS["carbon.cloudThreshold"] = {"app.feature.carbon_stock", "app.feature.vegetation_indices"}


def _parameter_feature(target: str) -> str | tuple[str, ...]:
    if target in SHARED_CARBON_PARAMETERS:
        return tuple(sorted(SHARED_CARBON_PARAMETERS[target]))
    if target.startswith("carbon.vegetation"):
        return "app.feature.vegetation_indices"
    if target == "carbon.landcoverDatasets":
        return "app.feature.land_cover"
    return {"carbon": "app.feature.carbon_stock", "lc_change": "app.feature.land_cover_change",
            "scenes": "app.feature.satellite_exploration", "crop": "app.feature.crop_monitoring"}[target.split(".")[0]]


_ACTIONS += [
    (target, "set_parameter", target, _parameter_feature(target), {"value": schema}, ["value"],
     ["owning module", "value from runtime catalog/state when applicable"])
    for target, schema in _PARAMETER_SPECS.items()
]
_ACTIONS += [
    ("module", "open_module", "navigation.module", None,
     {"value": {"type": "string", "enum": ["carbon", "lc-change", "imagery", "disaster", "crop-monitoring", "guide", "about"]}}, ["value"], []),
    ("map", "set_map_view", "map.main", None,
     {"center": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
      "zoom": {"type": "number", "minimum": 0, "maximum": 24}}, ["center", "zoom"], ["active map", "valid latitude/longitude"]),
    ("analysis_type", "set_analysis_type", "carbon.analysisType", None,
     {"value": {"type": "string", "enum": ["carbon", "landcover", "vegetation", "combined"]}}, ["value"], ["permitted analysis type"]),
    ("provider", "set_provider", "scenes.provider", "app.feature.satellite_exploration",
     {"value": {"type": "string"}}, ["value"], ["availableProviders"]),
    *[(f"{namespace}.dateRange", "set_date_range", f"{namespace}.dateRange", feature,
       {key: {"type": "string", "format": "date"} for key in ("start", "end")}, ["start", "end"], ["start <= end"])
      for namespace, feature in (("scenes", "app.feature.satellite_exploration"), ("crop", "app.feature.crop_monitoring"))],
    ("layer", "toggle_layer", "results.layer", None,
     {"value": {"type": "string"}, "visible": {"type": "boolean"}}, ["value", "visible"], ["availableLayers"]),
    ("clear_scenes", "clear_scenes", "scenes.layers", "app.feature.satellite_exploration", {}, [], []),
    ("scene_panel", "accordion", "scenes.results", "app.feature.satellite_exploration",
     {"open": {"type": "boolean"}}, ["open"], []),
    ("disaster_sources", "load_disaster_sources", "disaster.sources", "app.feature.disaster_mapping", {}, [], []),
    ("disaster_alerts", "load_disaster_alerts", "disaster.alerts", "app.feature.disaster_mapping", {}, [], []),
]

UI_ACTION_TARGETS: dict[str, set[str]] = {}
for _, action, target, *_ in _ACTIONS:
    UI_ACTION_TARGETS.setdefault(action, set()).add(target)


def application_action_catalog(allowed_features: set[str] | None = None) -> list[dict[str, Any]]:
    catalog = []
    for key, action, target, feature, properties, required, prerequisites in _ACTIONS:
        if feature is not None and allowed_features is not None and not (
            set(feature).intersection(allowed_features) if isinstance(feature, tuple) else feature in allowed_features
        ):
            continue
        if allowed_features is not None and action in {"open_module", "set_analysis_type"}:
            type_features = {
                "carbon": {"app.feature.carbon_stock"}, "landcover": {"app.feature.land_cover"},
                "vegetation": {"app.feature.vegetation_indices"},
                "combined": {"app.feature.carbon_stock", "app.feature.land_cover", "app.feature.vegetation_indices"},
            }
            module_features = {
                "carbon": {"app.feature.carbon_stock", "app.feature.land_cover", "app.feature.vegetation_indices"},
                "lc-change": {"app.feature.land_cover_change"}, "imagery": {"app.feature.satellite_exploration"},
                "disaster": {"app.feature.disaster_mapping"}, "crop-monitoring": {"app.feature.crop_monitoring"},
            }
            choices = [value for value in properties["value"]["enum"] if (
                type_features[value].issubset(allowed_features) if action == "set_analysis_type" else
                value not in module_features or bool(module_features[value].intersection(allowed_features))
            )]
            if not choices:
                continue
            properties = {"value": {**properties["value"], "enum": choices}}
        catalog.append({
            "id": key, "action": action, "target": target,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            "prerequisites": prerequisites, "status": "supported", "data_availability": "must be checked by backend for AOI/period",
            "coverage_status": "unverified" if action in {"run_analysis", "search_scenes", "show_scene"} else "not_applicable",
            "result": "owning React module state, result panel and map layers",
            "requires_confirmation": action == "run_analysis",
        })
    return catalog


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
  Keep the administrative level word the user typed ("Kota Semarang", "Kabupaten Semarang",
  "Kecamatan Tembalang") in the query: "Kota X" and "Kabupaten X" are different areas, and a bare
  name that matches several areas makes the app ask the user to choose. Never drop or swap the word.
  A select_aoi followed by search/analysis in the same queue is supported; failure stops the queue.
- use_active_aoi / aoi.active (no parameters): "gunakan AOI yang sedang aktif". Re-centres the map on the
  AOI already in context (context.aoi_name) without searching. If context.aoi is empty it fails; then
  ask the user which area to use instead of guessing.
- select_field / crop.field, parameters {"field_id":17}. Only use IDs in ui.crop.availableFieldIds;
  field selection also selects its AOI. Ask the user to choose a saved field/commodity if ambiguous.
- show_scene / scenes.scene may use {"scene_id":"latest","visible":true} AFTER search_scenes.
  The frontend resolves latest by actual acquisition time in the returned results. It is latest within
  the chosen date/cloud filters; mention truncation and never imply global catalog completeness.
- run_analysis / disaster.event requires event_type (flood/fire/landslide), before_start, before_end,
  after_start, after_end (ISO dates). Require before_start < before_end <= after_start < after_end.
  Ask for missing event dates; DEM is terrain context and cannot substitute for a flood impact map.
  Real /disaster/event-map handles coverage failures and the UI displays its raster, legend and metadata.
Use context.action_catalog for available actions, exact targets, parameter schemas and prerequisites.
For set_parameter always use the catalog target verbatim and parameters {"value": ...}; never
invent targets such as carbon.year, imagery.cloudMax or disaster.event_type. Provider selection
uses set_provider, date ranges use set_date_range, and event inputs belong in disaster.event.
carbon.loadOnly is a boolean: true loads the actual reference layer without model inference or
computed statistics; the UI also enables referenceOnly and clears the selected model. Keep an
AOI for this mode. Use carbon.directGlobal only for the separate global dataset mode. context.runtime reports backend initialization; an uninitialized Earth Engine cannot run GEE workflows. Do not promise availability when runtime/data is missing. Provider/dataset/model choices
must come from actual runtime catalogs. Open the owning module before changing parameters.
For vegetation prefer carbon.vegetationStartMonth / vegetationEndMonth / vegetationCloudThreshold
and report these actual vegetation settings, rather than carbon-only defaults. The shared
carbon.startMonth/endMonth/cloudThreshold setters apply to the selected analysis type.
Selecting vegetation automatically leaves carbon direct-load/reference modes. Use "action"
for application commands; the "type" discriminator is only for the legacy map/result actions.
In capability answers say "modul tersedia; cakupan data belum diverifikasi" for supported but
untested workflows. Never label them "Siap", or give a green check, just because the catalog
lists a supported endpoint or provider. A provider count is catalog size, not configured-provider
or data-coverage evidence. A failed carbon dataset says nothing about vegetation/land-cover
coverage. Disaster mapping requires explicit event dates; field monitoring requires a saved
field/commodity. Ask for missing inputs, and read actual results before describing vegetation
condition. Use the Indonesian term "longsor" for landslide.
Navigation alone never starts analysis. Execution requests with all prerequisites get a concrete
queue; costly run_analysis queues need confirmation. Do not claim execution success in a plan.
For available/relevant analyses, list supported actions separately from verified AOI data coverage,
run requested fully specified workflows, and ask only missing mandatory inputs. Never assume data
coverage just because an endpoint exists. Job IDs and statuses in context.jobs are backend reports;
do not invent percentages or cancellation support. Retain AOI, dates and field from current context.
"""
