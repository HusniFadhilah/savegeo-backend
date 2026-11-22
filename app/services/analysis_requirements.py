"""Shared provider requirements for HTTP and durable analysis adapters."""


def requires_earth_engine(kind: str, payload: dict) -> bool:
    if kind == "carbon_local":
        return payload.get("model_name") == "ibm_granite_geospatial_biomass"
    if kind == "carbon":
        from app.registries.carbon_dataset_registry import get_external_carbon_meta
        meta = get_external_carbon_meta(str(payload.get("reference_dataset") or ""))
        direct = bool(payload.get("reference_only")) or not payload.get("model_name")
        return not (direct and meta and meta.get("ingestion_method") in {"cog_rasterio", "soilgrids_rest"})
    return True
