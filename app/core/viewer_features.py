"""Feature-level Viewer access layered on top of the existing role-permission model."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models.admin_user import AdminUser
from app.db.models.role import Role
from app.db.models.user import User
from app.db.session import get_db


FEATURE_CODES: dict[str, str] = {
    "carbon_stock": "app.feature.carbon_stock",
    "land_cover": "app.feature.land_cover",
    "vegetation_indices": "app.feature.vegetation_indices",
    "land_cover_change": "app.feature.land_cover_change",
    "satellite_exploration": "app.feature.satellite_exploration",
    "disaster_mapping": "app.feature.disaster_mapping",
    "crop_monitoring": "app.feature.crop_monitoring",
}
VIEWER_FEATURES: tuple[tuple[str, str, str], ...] = (
    (FEATURE_CODES["carbon_stock"], "Estimasi stok karbon", "Estimasi dan ekspor hasil stok karbon."),
    (FEATURE_CODES["land_cover"], "Land Cover / LULC", "Klasifikasi tutupan lahan dan hasilnya."),
    (FEATURE_CODES["vegetation_indices"], "Indeks vegetasi", "Analisis indeks vegetasi dan deret waktunya."),
    (FEATURE_CODES["land_cover_change"], "Perubahan tutupan lahan", "Perbandingan tutupan lahan antarperiode."),
    (FEATURE_CODES["satellite_exploration"], "Eksplorasi scene satelit", "Pencarian scene, tampilan tile, dan unduhan."),
    (FEATURE_CODES["disaster_mapping"], "Pemetaan bencana", "Melihat data dan analisis bencana yang dipublikasikan."),
    (FEATURE_CODES["crop_monitoring"], "Crop Monitoring", "Pemantauan field, tanaman, dan hasil analisis."),
)
VIEWER_FEATURE_CODES = frozenset(code for code, _, _ in VIEWER_FEATURES)
DEFAULT_VIEWER_FEATURE_CODES = VIEWER_FEATURE_CODES
VIEWER_ROLE_NAME = "viewer"
VIEWER_FEATURES_MANAGE_PERMISSION = "viewer_features.manage"
FEATURE_ACCESS_DENIED_DETAIL = "Akses ke fitur ini tidak diizinkan. Hubungi administrator jika memerlukan akses."

GEOSPATIAL_MODULE_FEATURES: dict[str, str] = {
    "carbon": FEATURE_CODES["carbon_stock"],
    "carbon_stock": FEATURE_CODES["carbon_stock"],
    "landcover": FEATURE_CODES["land_cover"],
    "land_cover": FEATURE_CODES["land_cover"],
    "land-cover": FEATURE_CODES["land_cover"],
    "lulc": FEATURE_CODES["land_cover"],
    "landcover_change": FEATURE_CODES["land_cover_change"],
    "land_cover_change": FEATURE_CODES["land_cover_change"],
    "land-cover-change": FEATURE_CODES["land_cover_change"],
    "lc-change": FEATURE_CODES["land_cover_change"],
    "vegetation": FEATURE_CODES["vegetation_indices"],
    "vegetation_indices": FEATURE_CODES["vegetation_indices"],
    "ndvi": FEATURE_CODES["vegetation_indices"],
    "imagery": FEATURE_CODES["satellite_exploration"],
    "satellite": FEATURE_CODES["satellite_exploration"],
    "satellite_exploration": FEATURE_CODES["satellite_exploration"],
    "disaster": FEATURE_CODES["disaster_mapping"],
    "disaster_mapping": FEATURE_CODES["disaster_mapping"],
    "crop": FEATURE_CODES["crop_monitoring"],
    "crop_monitoring": FEATURE_CODES["crop_monitoring"],
}


def feature_code_for_module(module: str | None) -> str | None:
    if not isinstance(module, str):
        return None
    return GEOSPATIAL_MODULE_FEATURES.get(module.strip().casefold())


def is_viewer_admin(principal: AdminUser | User) -> bool:
    return isinstance(principal, User) or bool(
        isinstance(principal, AdminUser)
        and principal.role
        and principal.role.name
        and principal.role.name.strip().casefold() == VIEWER_ROLE_NAME
    )


def viewer_feature_codes(db: Session, principal: AdminUser | User) -> set[str]:
    """Resolve feature permissions from the current database role on each request."""
    if isinstance(principal, AdminUser):
        if not is_viewer_admin(principal):
            # Feature gates are additive for Viewers. Keep existing access for
            # Full Access Admin, limited Admin, and Geospatial Expert accounts.
            return set(VIEWER_FEATURE_CODES)
        role = principal.role
    else:
        # Public application users have no role column. Treat them as Viewer
        # and resolve the shared Viewer policy; missing role means deny all.
        role = db.query(Role).filter(Role.name.ilike(VIEWER_ROLE_NAME)).first()
    if role is None:
        return set()
    return {permission.code for permission in role.permissions if permission.code in VIEWER_FEATURE_CODES}


def ensure_feature_access(principal: AdminUser | User, code: str, db: Session) -> None:
    if code not in VIEWER_FEATURE_CODES:
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    if code not in viewer_feature_codes(db, principal):
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)


def require_feature(code: str):
    """FastAPI dependency for feature endpoints; reads live DB permissions."""
    from app.core.security import get_current_app_viewer

    def _dependency(
        principal: AdminUser | User = Depends(get_current_app_viewer),
        db: Session = Depends(get_db),
    ) -> AdminUser | User:
        ensure_feature_access(principal, code, db)
        return principal

    return _dependency


def require_any_feature(codes: set[str] | frozenset[str] = VIEWER_FEATURE_CODES):
    """Require at least one enabled application feature for shared app APIs."""
    from app.core.security import get_current_app_viewer

    def _dependency(
        principal: AdminUser | User = Depends(get_current_app_viewer),
        db: Session = Depends(get_db),
    ) -> AdminUser | User:
        if not (viewer_feature_codes(db, principal) & set(codes)):
            raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
        return principal

    return _dependency


JOB_TYPE_FEATURES: dict[str, str] = {
    "carbon": FEATURE_CODES["carbon_stock"],
    "carbon_local": FEATURE_CODES["carbon_stock"],
    "carbon_delta": FEATURE_CODES["carbon_stock"],
    "vegetation": FEATURE_CODES["vegetation_indices"],
    "landcover": FEATURE_CODES["land_cover"],
    "landcover_change": FEATURE_CODES["land_cover_change"],
    "landcover_transition": FEATURE_CODES["land_cover_change"],
    "landcover_change_map": FEATURE_CODES["land_cover_change"],
    "landcover_hotspots": FEATURE_CODES["land_cover_change"],
    "landcover_fusion": FEATURE_CODES["land_cover_change"],
    "crop_monitoring": FEATURE_CODES["crop_monitoring"],
}


def ensure_job_feature_access(
    principal: AdminUser | User,
    job_type: str,
    db: Session,
    payload: dict | None = None,
) -> None:
    if job_type == "geotiff":
        ensure_export_feature_access(principal, payload or {}, db)
        return
    code = JOB_TYPE_FEATURES.get(job_type)
    if code is None:
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    ensure_feature_access(principal, code, db)


def ensure_export_feature_access(principal: AdminUser | User, payload: dict, db: Session) -> None:
    layer_type = payload.get("layer_type")
    if not isinstance(layer_type, str):
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    code_by_layer = {
        "vegetation": FEATURE_CODES["vegetation_indices"],
        "rgb": FEATURE_CODES["satellite_exploration"],
        "landcover": FEATURE_CODES["land_cover"],
        "carbon": FEATURE_CODES["carbon_stock"],
    }
    code = code_by_layer.get(layer_type.strip().casefold())
    if code is None:
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
    ensure_feature_access(principal, code, db)


WORKFLOW_CATEGORY_FEATURES: dict[str, str] = {
    "carbon": FEATURE_CODES["carbon_stock"],
    "vegetation": FEATURE_CODES["vegetation_indices"],
    "crop": FEATURE_CODES["crop_monitoring"],
    "land-cover": FEATURE_CODES["land_cover"],
    "disaster": FEATURE_CODES["disaster_mapping"],
}
WORKFLOW_NODE_FEATURES: dict[str, str] = {
    "load_imagery": FEATURE_CODES["satellite_exploration"],
    "load_multitemporal_imagery": FEATURE_CODES["satellite_exploration"],
    "load_before_imagery": FEATURE_CODES["satellite_exploration"],
    "load_after_imagery": FEATURE_CODES["satellite_exploration"],
    "ndvi": FEATURE_CODES["vegetation_indices"],
    "evi": FEATURE_CODES["vegetation_indices"],
    "ndwi": FEATURE_CODES["vegetation_indices"],
    "savi": FEATURE_CODES["vegetation_indices"],
    "vegetation_index": FEATURE_CODES["vegetation_indices"],
    "time_series": FEATURE_CODES["vegetation_indices"],
    "land_cover": FEATURE_CODES["land_cover"],
    "classification": FEATURE_CODES["land_cover"],
    "reclassify": FEATURE_CODES["land_cover"],
    "carbon_model": FEATURE_CODES["carbon_stock"],
    "load_field_boundary": FEATURE_CODES["crop_monitoring"],
    "change_detection": FEATURE_CODES["land_cover_change"],
    "severity_classification": FEATURE_CODES["disaster_mapping"],
    "hotspot_3d": FEATURE_CODES["disaster_mapping"],
}
WORKFLOW_NEUTRAL_NODE_TYPES = frozenset({
    "load_aoi", "load_raster", "load_dem", "cloud_mask", "clip", "band_math",
    "slope", "aspect", "hillshade", "histogram", "raster_statistics", "export",
    "export_geotiff", "export_cog", "export_geojson", "export_report", "export_map",
    "add_map_layer", "add_3d_layer", "zonal_statistics",
})
_UNMAPPED_WORKFLOW_REQUIREMENT = "app.feature.unmapped_workflow_node"


def workflow_required_feature_codes(workflow: dict) -> set[str]:
    """Return all feature grants needed to read or operate a workflow."""
    required: set[str] = set()
    category = str(workflow.get("category", "general")).strip().casefold()
    if category != "general" and category not in WORKFLOW_CATEGORY_FEATURES:
        return {_UNMAPPED_WORKFLOW_REQUIREMENT}
    if code := WORKFLOW_CATEGORY_FEATURES.get(category):
        required.add(code)
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        return {_UNMAPPED_WORKFLOW_REQUIREMENT}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            return {_UNMAPPED_WORKFLOW_REQUIREMENT}
        node_type = node["type"].strip().casefold()
        if code := WORKFLOW_NODE_FEATURES.get(node_type):
            required.add(code)
        elif node_type not in WORKFLOW_NEUTRAL_NODE_TYPES:
            return {_UNMAPPED_WORKFLOW_REQUIREMENT}
    return required


def ensure_workflow_feature_access(principal: AdminUser | User, workflow: dict, db: Session) -> None:
    required = workflow_required_feature_codes(workflow)
    if not required.issubset(viewer_feature_codes(db, principal)):
        raise HTTPException(status_code=403, detail=FEATURE_ACCESS_DENIED_DETAIL)
