"""Admin API for the Disaster Intelligence Dashboard - event/AOI/imagery/
analysis/hotspot CRUD + QC + audit trail. See
`docs/disaster-redesign-contract.md` section A for the full route contract
this file implements verbatim.

Router prefix is `/admin` (not `/admin/disasters`) because the contract's own
route table mixes `/admin/disasters/...`, `/admin/analyses/...`, and
`/admin/hotspots/...` paths on a single router - a single APIRouter `prefix`
is prepended to every route registered on it, so the only way to reproduce
all three literal path shapes exactly is to prefix at `/admin` and spell out
each sub-path in full below. (The contract doc's one-line router example,
`prefix="/admin/disasters"`, would make the `/admin/analyses/...` and
`/admin/hotspots/...` rows unreachable under that same prefix - resolved in
favor of the route table, since that's what both frontend agents code
against.)

Not registered in `app/api/router.py` / `app/main.py` here - central wiring
happens in the integration pass per the contract doc and the task brief for
this file.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_current_admin_panel, require_permission
from app.db.models.admin_user import AdminUser
from app.db.models.analysis_run import RUN_STATUSES
from app.db.models.audit_log import AuditLog
from app.db.models.disaster_event import DISASTER_TYPES, EVENT_STATUSES, SEVERITIES, DisasterEvent
from app.db.models.hotspot import IMPACT_LEVELS, Hotspot
from app.db.session import get_db
from app.registries.disaster_model_registry import list_models
from app.repositories import disaster_repo
from app.services import (
    audit_service,
    disaster_capability_service,
    disaster_analysis_service,
    firms_service,
    local_imagery_tile_service,
    wildfire_hotspot_service,
)
from app.services.gee_common import AnalysisError, public_analysis_error
from app.services.geo_utils import bbox_and_centroid, estimate_area_ha

router = APIRouter(prefix="/admin", tags=["admin-disaster"])


# -- Request schemas (local to this router - mirrors app/schemas/admin.py's
#    Optional-field style for partial-update bodies, e.g. ModelUpdateRequest) --


class EventCreateRequest(BaseModel):
    name: str
    disaster_type: str
    location_name: str | None = None
    province: list[str] | None = None
    district: list[str] | None = None
    event_date: dt.date | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    severity: str | None = None
    description: str | None = None
    source: str | None = None
    thumbnail: str | None = None
    slug: str | None = None
    short_title: str | None = None
    monitoring_from: dt.date | None = None
    monitoring_to: dt.date | None = None
    year: int | None = None
    country_code: str | None = "ID"
    province_codes: list[str] | None = None
    city_codes: list[str] | None = None
    bbox: list[float] | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    default_zoom: int | None = None
    source_ids: list[str] | None = None
    hotspot_dataset_ids: list[str] | None = None
    burned_area_dataset_ids: list[str] | None = None
    boundary_source: str | None = None
    methodology: str | None = None
    limitations: list[str] | None = None
    is_featured: bool = False
    is_public: bool = False


class EventUpdateRequest(BaseModel):
    name: str | None = None
    disaster_type: str | None = None
    location_name: str | None = None
    province: list[str] | None = None
    district: list[str] | None = None
    event_date: dt.date | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    status: str | None = None
    severity: str | None = None
    description: str | None = None
    source: str | None = None
    thumbnail: str | None = None
    slug: str | None = None
    short_title: str | None = None
    monitoring_from: dt.date | None = None
    monitoring_to: dt.date | None = None
    year: int | None = None
    country_code: str | None = None
    province_codes: list[str] | None = None
    city_codes: list[str] | None = None
    bbox: list[float] | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    default_zoom: int | None = None
    source_ids: list[str] | None = None
    hotspot_dataset_ids: list[str] | None = None
    burned_area_dataset_ids: list[str] | None = None
    boundary_source: str | None = None
    methodology: str | None = None
    limitations: list[str] | None = None
    is_featured: bool | None = None
    is_public: bool | None = None


class AoiCreateRequest(BaseModel):
    geojson: dict
    source: str | None = "draw"


class ImageryCreateRequest(BaseModel):
    phase: str
    satellite: str
    acquisition_date: dt.date
    sensor: str | None = None
    resolution_m: float | None = None
    cloud_coverage_pct: float | None = None
    data_source: str | None = None
    scene_id: str | None = None
    is_primary: bool | None = False
    # "gee" (default) - preview_tile_url is a ready GEE getMapId() template.
    # "local_upload" - preview_tile_url is filled in AFTER creation (its
    # template embeds this row's own id, see disaster_repo.set_preview_tile_url)
    # from a raster already ingested at local_file_path (see
    # app/services/local_imagery_tile_service.ensure_cog - COG conversion is a
    # separate offline step, this endpoint doesn't upload/convert a file itself).
    source_kind: str | None = "gee"
    local_file_path: str | None = None
    preview_tile_url: str | None = None


class AnalysisCreateRequest(BaseModel):
    model_id: str | None = None
    aoi_id: int
    pre_imagery_id: int | None = None
    post_imagery_id: int | None = None
    parameters: dict | None = None


class RunStatusUpdateRequest(BaseModel):
    status: str


class ResultUpdateRequest(BaseModel):
    statistics: dict | None = None
    legend: list | None = None
    confidence_summary: dict | None = None


class HotspotCreateRequest(BaseModel):
    name: str
    impact_level: str
    geojson: dict
    analysis_result_id: int | None = None
    stats: dict | None = None
    is_published: bool | None = False


class HotspotUpdateRequest(BaseModel):
    name: str | None = None
    impact_level: str | None = None
    geojson: dict | None = None
    analysis_result_id: int | None = None
    stats: dict | None = None
    is_published: bool | None = None


class WildfireSyncRequest(BaseModel):
    from_date: dt.date | None = None
    to_date: dt.date | None = None


# -- helpers --


def _get_event_or_404(db: Session, event_id: int):
    event = disaster_repo.get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Disaster event not found")
    return event


def _safe_file_stem(filename: str) -> str:
    stem = Path(filename).stem or "upload"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "upload"


def _select_zip_geotiff(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".tif", ".tiff"))]
    if not names:
        raise ValueError("ZIP tidak berisi file .tif/.tiff")

    def usable(name: str) -> bool:
        lower = Path(name).name.lower()
        return not any(marker in lower for marker in ("-pan", "_pan", "-mask", "_mask"))

    preferred = [n for n in names if usable(n) and "_ortho" in Path(n).name.lower()]
    preferred += [n for n in names if usable(n) and "pregeoreferenced" in Path(n).name.lower()]
    preferred += [n for n in names if usable(n)]
    selected = preferred[0] if preferred else names[0]
    return f"/vsizip/{zip_path.resolve().as_posix()}/{selected}"


def _get_run_or_404(db: Session, run_id: int):
    run = disaster_repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Analysis run not found")
    return run


# -- Disaster events --------------------------------------------------------


@router.post("/disasters", status_code=201)
def admin_create_event(
    payload: EventCreateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.create")),
    db: Session = Depends(get_db),
):
    if payload.disaster_type not in DISASTER_TYPES:
        raise HTTPException(status_code=400, detail=f"disaster_type harus salah satu dari {DISASTER_TYPES}")
    if payload.severity is not None and payload.severity not in SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity harus salah satu dari {SEVERITIES}")
    if payload.bbox is not None and len(payload.bbox) != 4:
        raise HTTPException(status_code=400, detail="bbox harus berisi [west, south, east, north]")

    event = disaster_repo.create_event(db, payload.model_dump(), admin.id)
    audit_service.log_audit(db, admin.id, "disaster_event.create", "disaster_event", str(event.id), detail={"name": event.name})
    return event.to_dict()


@router.get("/disasters")
def admin_list_events(
    status: str | None = None,
    disaster_type: str | None = None,
    severity: str | None = None,
    year: int | None = None,
    search: str | None = None,
    admin: AdminUser = Depends(require_permission("disaster.read")),
    db: Session = Depends(get_db),
):
    events = disaster_repo.list_events(
        db,
        published_only=False,
        status=status,
        disaster_type=disaster_type,
        severity=severity,
        year=year,
        search=search,
    )
    return {"events": [e.to_dict() for e in events]}


@router.get("/disasters/models")
def admin_list_disaster_models(event_id: int | None = None, admin: AdminUser = Depends(require_permission("disaster.read")), db: Session = Depends(get_db)):
    event = _get_event_or_404(db, event_id) if event_id else None
    models = list_models(enabled_only=False, disaster_type=event.disaster_type if event else None)
    settings = get_settings()
    configured = bool(settings.gee_service_account and settings.gee_key_file and Path(settings.gee_key_file).is_file())
    models = [{**m, "configured": (not m.get("requires_earth_engine", True) or configured) and m["enabled"],
        "capability_status": disaster_capability_service.model_state(m),
        "availability_reason": (
            "Citra pada tanggal/AOI akan diperiksa saat analisis"
            if (not m.get("requires_earth_engine", True) or configured) and m["enabled"]
            else m.get("availability_reason") or "Kredensial GEE atau implementasi model belum tersedia"
        ),
        "recommended": bool(m.get("multiclass") and configured and m["enabled"])} for m in models]
    if event:
        aoi = disaster_repo.get_active_aoi(db, event_id)
        pre = disaster_repo.list_imagery(db, event_id, "pre")
        post = disaster_repo.list_imagery(db, event_id, "post")
        recommended = False
        for model in models:
            model["recommended"] = False
            model["inputs_ready"] = False
            reason = "AOI dan imagery pre/post yang kompatibel belum tersedia"
            if aoi:
                if not model.get("requires_pre") and not model.get("requires_post"):
                    try:
                        disaster_analysis_service.validate_inputs(db, event_id, aoi.id, None, None, model["model_id"])
                        model["inputs_ready"] = True
                        reason = "AOI tersedia; periode/sumber produk diisi pada parameter run"
                    except AnalysisError as exc:
                        reason = str(exc)
                else:
                    for before in pre:
                        for after in post:
                            try:
                                disaster_analysis_service.validate_inputs(db, event_id, aoi.id, before.id, after.id, model["model_id"])
                                model["inputs_ready"] = True
                                model["default_pre_imagery_id"] = before.id
                                model["default_post_imagery_id"] = after.id
                                break
                            except AnalysisError as exc:
                                reason = str(exc)
                        if model["inputs_ready"]:
                            break
            if not model["inputs_ready"]:
                model["availability_reason"] = reason
            elif model["configured"] and not recommended:
                model["recommended"] = recommended = True
    return {"models": models}


@router.get("/disasters/{id}")
def admin_get_event(id: int, admin: AdminUser = Depends(require_permission("disaster.read")), db: Session = Depends(get_db)):
    event = _get_event_or_404(db, id)
    aoi = disaster_repo.get_active_aoi(db, id)
    pre_imagery = disaster_repo.list_imagery(db, id, "pre")
    post_imagery = disaster_repo.list_imagery(db, id, "post")
    runs = disaster_repo.list_runs_for_event(db, id)
    run_entries = []
    for run in runs:
        result = disaster_repo.get_result_for_run(db, run.id)
        run_entries.append({"run": run.to_dict(), "result": result.to_dict() if result else None})

    return {
        "event": event.to_dict(),
        "aoi": aoi.to_dict() if aoi else None,
        "imagery": {
            "pre": [i.to_dict() for i in pre_imagery],
            "post": [i.to_dict() for i in post_imagery],
        },
        "runs": run_entries,
    }


@router.patch("/disasters/{id}")
def admin_update_event(
    id: int,
    payload: EventUpdateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.update")),
    db: Session = Depends(get_db),
):
    event = _get_event_or_404(db, id)
    changes = payload.model_dump(exclude_unset=True)
    if "disaster_type" in changes and changes["disaster_type"] not in DISASTER_TYPES:
        raise HTTPException(status_code=400, detail=f"disaster_type harus salah satu dari {DISASTER_TYPES}")
    if "status" in changes and changes["status"] not in EVENT_STATUSES:
        raise HTTPException(status_code=400, detail=f"status harus salah satu dari {EVENT_STATUSES}")
    if "severity" in changes and changes["severity"] is not None and changes["severity"] not in SEVERITIES:
        raise HTTPException(status_code=400, detail=f"severity harus salah satu dari {SEVERITIES}")
    if "bbox" in changes and changes["bbox"] is not None and len(changes["bbox"]) != 4:
        raise HTTPException(status_code=400, detail="bbox harus berisi [west, south, east, north]")
    if changes.get("status") == "published" and event.disaster_type == "forest_fire":
        minimum = (event.slug or changes.get("slug"), event.source, event.start_date or changes.get("start_date"), event.province or changes.get("province"))
        if not all(minimum):
            raise HTTPException(status_code=400, detail="Event karhutla memerlukan slug, periode, cakupan wilayah, dan sumber sebelum dipublikasikan")

    if changes.get("status") == "published":
        import copy

        candidate = copy.copy(event)
        for field, value in changes.items():
            setattr(candidate, field, value)
        publication = disaster_capability_service.event_publication_check(db, candidate)
        if not publication.allowed:
            raise HTTPException(status_code=400, detail={"message": "Event belum memenuhi publication gate", "reasons": list(publication.reasons)})

    event = disaster_repo.update_event(db, event, changes)
    audit_service.log_audit(db, admin.id, "disaster_event.update", "disaster_event", str(event.id), detail=changes)
    return event.to_dict()


@router.delete("/disasters/{id}")
def admin_delete_event(
    id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.delete")),
    db: Session = Depends(get_db),
):
    event = _get_event_or_404(db, id)
    name = event.name
    disaster_repo.delete_event(db, event)
    audit_service.log_audit(db, admin.id, "disaster_event.delete", "disaster_event", str(id), detail={"name": name})
    return {"message": "deleted"}


# -- AOI ----------------------------------------------------------------------


@router.post("/disasters/{id}/aoi", status_code=201)
def admin_create_aoi(
    id: int,
    payload: AoiCreateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.aoi.write")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    area_ha = estimate_area_ha(payload.geojson)
    bbox, centroid = bbox_and_centroid(payload.geojson)
    data = {
        "geojson": payload.geojson,
        "area_ha": area_ha,
        "bbox": bbox,
        "centroid": centroid,
        "source": payload.source or "draw",
    }
    aoi = disaster_repo.create_aoi(db, id, data)
    audit_service.log_audit(db, admin.id, "disaster_aoi.create", "disaster_event", str(id), detail={"aoi_id": aoi.id, "area_ha": area_ha})
    return aoi.to_dict()


# -- Imagery --------------------------------------------------------------


@router.get("/disasters/{id}/imagery")
def admin_list_imagery(
    id: int,
    phase: str | None = None,
    admin: AdminUser = Depends(require_permission("disaster.read")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    imagery = disaster_repo.list_imagery(db, id, phase)
    return {"imagery": [i.to_dict() for i in imagery]}


@router.post("/disasters/{id}/imagery", status_code=201)
def admin_add_imagery(
    id: int,
    payload: ImageryCreateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.imagery.write")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    if payload.phase not in ("pre", "post"):
        raise HTTPException(status_code=400, detail="phase harus 'pre' atau 'post'")
    img = disaster_repo.add_imagery(db, id, payload.model_dump())
    audit_service.log_audit(
        db, admin.id, "disaster_imagery.create", "disaster_event", str(id),
        detail={"imagery_id": img.id, "phase": img.phase, "satellite": img.satellite},
    )
    return img.to_dict()


@router.post("/disasters/{id}/imagery/upload", status_code=201)
async def admin_upload_imagery(
    id: int,
    phase: str = Form(...),
    satellite: str = Form("Custom GeoTIFF"),
    acquisition_date: dt.date = Form(...),
    sensor: str | None = Form(None),
    resolution_m: float | None = Form(None),
    cloud_coverage_pct: float | None = Form(None),
    data_source: str | None = Form(None),
    is_primary: bool = Form(False),
    file: UploadFile = File(...),
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.imagery.write")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    if phase not in ("pre", "post"):
        raise HTTPException(status_code=400, detail="phase harus 'pre' atau 'post'")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".tif", ".tiff", ".zip"}:
        raise HTTPException(status_code=400, detail="File harus GeoTIFF (.tif/.tiff) atau ZIP berisi GeoTIFF")

    settings = get_settings()
    event_dir = settings.disaster_raster_path / f"event_{id}"
    event_dir.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex[:12]
    stem = _safe_file_stem(file.filename or "geotiff")
    raw_path = event_dir / f"{stem}_{token}{suffix}"
    cog_path = event_dir / f"{stem}_{token}_cog.tif"

    max_bytes = 2 * 1024 * 1024 * 1024
    written = 0
    try:
        with raw_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=400, detail="File GeoTIFF/ZIP terlalu besar (maks 2 GB)")
                out.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="File GeoTIFF/ZIP kosong")

        source_path = str(raw_path)
        selected_zip_member = None
        if suffix == ".zip":
            try:
                source_path = _select_zip_geotiff(raw_path)
                selected_zip_member = source_path.rsplit("/", 1)[-1]
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Gagal menemukan GeoTIFF utama di ZIP: {exc}") from exc

        local_imagery_tile_service.ensure_cog(source_path, str(cog_path))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Gagal membaca/konversi GeoTIFF: {exc}") from exc

    img = disaster_repo.add_imagery(
        db,
        id,
        {
            "phase": phase,
            "satellite": satellite,
            "acquisition_date": acquisition_date,
            "sensor": sensor,
            "resolution_m": resolution_m,
            "cloud_coverage_pct": cloud_coverage_pct,
            "data_source": data_source or file.filename or "Local GeoTIFF upload",
            "scene_id": None,
            "is_primary": is_primary,
            "source_kind": "local_upload",
            "local_file_path": str(cog_path.resolve()),
        },
    )
    img = disaster_repo.set_preview_tile_url(
        db,
        img.id,
        # The admin route is only for draft previews. Published user views
        # must use the public disaster tile route, otherwise the post layer
        # silently returns 401 for ordinary disaster viewers.
        f"/api/disasters/imagery-tiles/{img.id}/{{z}}/{{x}}/{{y}}.png",
    )
    audit_service.log_audit(
        db,
        admin.id,
        "disaster_imagery.upload",
        "disaster_event",
        str(id),
        detail={
            "imagery_id": img.id,
            "phase": img.phase,
            "satellite": img.satellite,
            "filename": file.filename,
            "bytes": written,
            "selected_zip_member": selected_zip_member,
            "local_file_path": str(cog_path.resolve()),
        },
    )
    return img.to_dict()


@router.post("/disasters/{id}/imagery/{imagery_id}/primary")
def admin_set_primary_imagery(
    id: int,
    imagery_id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.imagery.write")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    try:
        img = disaster_repo.set_primary_imagery(db, id, imagery_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=public_analysis_error(exc))
    audit_service.log_audit(
        db, admin.id, "disaster_imagery.set_primary", "disaster_event", str(id),
        detail={"imagery_id": img.id, "phase": img.phase},
    )
    return img.to_dict()


# -- Local raster tile preview (admin-only, unpublished OK) ---------------


@router.get("/disasters/imagery-tiles/{imagery_id}/{z}/{x}/{y}.png")
def admin_local_imagery_tile(
    imagery_id: int,
    z: int,
    x: int,
    y: int,
    admin: AdminUser = Depends(require_permission("disaster.read")),
    db: Session = Depends(get_db),
):
    """Admin preview of a `source_kind="local_upload"` raster's tiles before
    the event/result is published - Admin needs to check the imagery looks
    right in AnalysisReview/ImageryManager ahead of the User-facing route
    below, which 404s on anything not yet published."""
    img = disaster_repo.get_imagery(db, imagery_id)
    if img is None or img.source_kind != "local_upload" or not img.local_file_path:
        raise HTTPException(status_code=404, detail="Local imagery not found")
    # local_file_path is stored as an absolute path under settings.disaster_raster_path
    # (see local_imagery_tile_service / scripts/ingest_ntt_earthquake.py) - not joined
    # with upload_dir, that's a separate unrelated storage location.
    png = local_imagery_tile_service.render_tile(img.local_file_path, z, x, y)
    if png is None:
        raise HTTPException(status_code=404, detail="Tile out of coverage")
    return Response(content=png, media_type="image/png")


# -- Analyses (runs/results) ---------------------------------------------


@router.post("/disasters/{id}/analyses", status_code=201)
def admin_create_analysis(
    id: int,
    payload: AnalysisCreateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.analysis.configure")),
    db: Session = Depends(get_db),
):
    event = _get_event_or_404(db, id)
    model_id = payload.model_id
    if not model_id:
        candidates = list_models(enabled_only=True, disaster_type=event.disaster_type)
        if not candidates:
            raise HTTPException(status_code=400, detail="Tidak ada model aktif yang mendukung bencana ini")
        for candidate in candidates:
            try:
                disaster_analysis_service.validate_inputs(db, id, payload.aoi_id, payload.pre_imagery_id, payload.post_imagery_id, candidate["model_id"])
                model_id = candidate["model_id"]
                break
            except AnalysisError:
                continue
        if not model_id:
            raise HTTPException(status_code=400, detail="Tidak ada model aktif yang kompatibel dengan input terpilih")
    try:
        model, _, _, _ = disaster_analysis_service.validate_inputs(db, id, payload.aoi_id,
            payload.pre_imagery_id, payload.post_imagery_id, model_id)
    except AnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc))
    run = disaster_repo.create_run(db, id, {**payload.model_dump(), "model_id": model_id, "model_version": model["version"]}, admin.id)
    audit_service.log_audit(
        db, admin.id, "disaster_analysis.create", "disaster_event", str(id),
        detail={"run_id": run.id, "model_id": run.model_id, "aoi_id": run.aoi_id},
    )
    return run.to_dict()


@router.get("/disasters/{id}/analyses")
def admin_list_analyses(id: int, admin: AdminUser = Depends(require_permission("disaster.read")), db: Session = Depends(get_db)):
    _get_event_or_404(db, id)
    runs = disaster_repo.list_runs_for_event(db, id)
    entries = []
    for run in runs:
        result = disaster_repo.get_result_for_run(db, run.id)
        entries.append({"run": run.to_dict(), "result": result.to_dict() if result else None})
    return {"runs": entries}


@router.post("/analyses/{run_id}/run")
def admin_run_analysis(
    run_id: int,
    force: bool = False,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.analysis.run")),
    db: Session = Depends(get_db),
):
    try:
        outcome = disaster_analysis_service.run_analysis(db, run_id, force=force)
    except AnalysisError as e:
        raise HTTPException(status_code=e.status_code, detail=public_analysis_error(e))

    audit_service.log_audit(
        db, admin.id, "disaster_analysis.run", "disaster_event", str(outcome["run"]["event_id"]),
        detail={"run_id": run_id, "model_id": outcome["run"]["model_id"], "status": outcome["run"]["status"]},
    )
    return outcome


@router.patch("/analyses/{run_id}/status")
def admin_update_analysis_status(
    run_id: int,
    payload: RunStatusUpdateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.analysis.run")),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    if payload.status not in RUN_STATUSES:
        raise HTTPException(status_code=400, detail=f"status harus salah satu dari {RUN_STATUSES}")
    if payload.status != "review_required" or run.status not in ("completed", "review_required"):
        raise HTTPException(status_code=400, detail="Status eksekusi dikelola oleh analisis; gunakan aksi publikasi untuk menerbitkan hasil")
    run.status = payload.status
    db.commit()
    db.refresh(run)
    audit_service.log_audit(
        db, admin.id, "disaster_analysis.status_update", "disaster_event", str(run.event_id),
        detail={"run_id": run_id, "status": payload.status},
    )
    return run.to_dict()


@router.post("/analyses/{run_id}/publish")
def admin_publish_result(
    run_id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.analysis.publish")),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    result = disaster_repo.get_result_for_run(db, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No result exists yet for this run")
    if run.status not in ("completed", "review_required", "published") or not (result.tile_url or result.features):
        raise HTTPException(status_code=400, detail="Hasil gagal/belum selesai tidak dapat dipublikasikan")
    capability = disaster_capability_service.validate_persisted_result(
        db, disaster_repo.get_event(db, run.event_id), run, result, require_published=False
    )
    if not capability.allowed:
        raise HTTPException(status_code=400, detail={"message": "Hasil belum memenuhi publication gate", "reasons": list(capability.reasons)})
    run.status = "published"
    result = disaster_repo.publish_result(db, result, admin.id)
    audit_service.log_audit(
        db, admin.id, "disaster_result.publish", "disaster_event", str(run.event_id),
        detail={"run_id": run_id, "result_id": result.id},
    )
    return result.to_dict()


@router.post("/analyses/{run_id}/unpublish")
def admin_unpublish_result(
    run_id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.analysis.unpublish")),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    result = disaster_repo.get_result_for_run(db, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No result exists yet for this run")
    result = disaster_repo.unpublish_result(db, result)
    audit_service.log_audit(
        db, admin.id, "disaster_result.unpublish", "disaster_event", str(run.event_id),
        detail={"run_id": run_id, "result_id": result.id},
    )
    return result.to_dict()


@router.patch("/analyses/{run_id}/result")
def admin_update_result(
    run_id: int,
    payload: ResultUpdateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.result.write")),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    result = disaster_repo.get_result_for_run(db, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No result exists yet for this run")
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(result, field, value)
    db.commit()
    db.refresh(result)
    audit_service.log_audit(
        db, admin.id, "disaster_result.update", "disaster_event", str(run.event_id),
        detail={"run_id": run_id, "result_id": result.id, "fields": list(changes.keys())},
    )
    return result.to_dict()


@router.delete("/analyses/{run_id}/result")
def admin_delete_result(
    run_id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.result.delete")),
    db: Session = Depends(get_db),
):
    run = _get_run_or_404(db, run_id)
    result = disaster_repo.get_result_for_run(db, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="No result exists yet for this run")
    db.delete(result)
    db.commit()
    audit_service.log_audit(
        db, admin.id, "disaster_result.delete", "disaster_event", str(run.event_id),
        detail={"run_id": run_id},
    )
    return {"message": "deleted"}


# -- QC ---------------------------------------------------------------------


@router.get("/disasters/{id}/qc")
def admin_qc_summary(id: int, admin: AdminUser = Depends(require_permission("disaster.read")), db: Session = Depends(get_db)):
    event = _get_event_or_404(db, id)
    aoi = disaster_repo.get_active_aoi(db, id)
    pre_imagery = disaster_repo.get_primary_imagery(db, id, "pre")
    post_imagery = disaster_repo.get_primary_imagery(db, id, "post")
    runs = disaster_repo.list_runs_for_event(db, id)

    analyses = []
    for run in runs:
        result = disaster_repo.get_result_for_run(db, run.id)
        analyses.append({
            "model_id": run.model_id,
            "status": run.status,
            "has_statistics": bool(result and result.statistics),
            "has_legend": bool(result and result.legend),
            "has_confidence": bool(result and result.confidence_summary),
        })

    ready_to_publish = bool(aoi) and bool(pre_imagery) and bool(post_imagery) and any(a["has_statistics"] for a in analyses)

    return {
        "aoi_configured": bool(aoi),
        "pre_imagery_available": bool(pre_imagery),
        "post_imagery_available": bool(post_imagery),
        "analyses": analyses,
        "ready_to_publish": ready_to_publish,
        "readiness": disaster_capability_service.readiness_matrix(db, event),
    }


# -- Hotspots -----------------------------------------------------------------


@router.post("/disasters/wildfires/{slug}/sync")
def admin_sync_wildfire_hotspots(
    slug: str,
    payload: WildfireSyncRequest | None = None,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.update")),
    db: Session = Depends(get_db),
):
    event = db.execute(
        select(DisasterEvent).where(
            DisasterEvent.slug == slug,
            DisasterEvent.disaster_type == "forest_fire",
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Wildfire event not found")
    try:
        result = wildfire_hotspot_service.sync_event_hotspots(
            db,
            event,
            from_date=payload.from_date if payload else None,
            to_date=payload.to_date if payload else None,
        )
    except firms_service.FirmsRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=public_analysis_error(exc)) from exc
    audit_service.log_audit(
        db,
        admin.id,
        "wildfire_hotspots.sync",
        "disaster_event",
        str(event.id),
        detail={"slug": slug, "from": result["from"], "to": result["to"], "stored": result["stored"]},
    )
    return result


@router.get("/disasters/{id}/hotspots")
def admin_list_hotspots(id: int, admin: AdminUser = Depends(require_permission("disaster.read")), db: Session = Depends(get_db)):
    _get_event_or_404(db, id)
    hotspots = disaster_repo.list_hotspots(db, id, published_only=False)
    return {"hotspots": [h.to_dict() for h in hotspots]}


@router.post("/disasters/{id}/hotspots", status_code=201)
def admin_create_hotspot(
    id: int,
    payload: HotspotCreateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.result.write")),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, id)
    if payload.impact_level not in IMPACT_LEVELS:
        raise HTTPException(status_code=400, detail=f"impact_level harus salah satu dari {IMPACT_LEVELS}")
    hotspot = disaster_repo.create_hotspot(db, id, payload.model_dump(), admin.id)
    audit_service.log_audit(
        db, admin.id, "disaster_hotspot.create", "disaster_event", str(id),
        detail={"hotspot_id": hotspot.id, "name": hotspot.name},
    )
    return hotspot.to_dict()


def _get_hotspot_or_404(db: Session, hotspot_id: int) -> Hotspot:
    hotspot = db.get(Hotspot, hotspot_id)
    if hotspot is None:
        raise HTTPException(status_code=404, detail="Hotspot not found")
    return hotspot


@router.patch("/hotspots/{hotspot_id}")
def admin_update_hotspot(
    hotspot_id: int,
    payload: HotspotUpdateRequest,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.result.write")),
    db: Session = Depends(get_db),
):
    hotspot = _get_hotspot_or_404(db, hotspot_id)
    changes = payload.model_dump(exclude_unset=True)
    if "impact_level" in changes and changes["impact_level"] not in IMPACT_LEVELS:
        raise HTTPException(status_code=400, detail=f"impact_level harus salah satu dari {IMPACT_LEVELS}")
    for field, value in changes.items():
        setattr(hotspot, field, value)
    db.commit()
    db.refresh(hotspot)
    audit_service.log_audit(
        db, admin.id, "disaster_hotspot.update", "disaster_event", str(hotspot.event_id),
        detail={"hotspot_id": hotspot_id, "fields": list(changes.keys())},
    )
    return hotspot.to_dict()


@router.delete("/hotspots/{hotspot_id}")
def admin_delete_hotspot(
    hotspot_id: int,
    admin: AdminUser = Depends(get_current_admin_panel),
    _perm: AdminUser = Depends(require_permission("disaster.result.delete")),
    db: Session = Depends(get_db),
):
    hotspot = _get_hotspot_or_404(db, hotspot_id)
    event_id, name = hotspot.event_id, hotspot.name
    db.delete(hotspot)
    db.commit()
    audit_service.log_audit(
        db, admin.id, "disaster_hotspot.delete", "disaster_event", str(event_id),
        detail={"hotspot_id": hotspot_id, "name": name},
    )
    return {"message": "deleted"}


# -- Audit trail ---------------------------------------------------------


@router.get("/disasters/{id}/audit")
def admin_event_audit(id: int, admin: AdminUser = Depends(require_permission("audit.read")), db: Session = Depends(get_db)):
    _get_event_or_404(db, id)
    logs = (
        db.query(AuditLog)
        .filter_by(resource_type="disaster_event", resource_id=str(id))
        .order_by(AuditLog.created_at.desc())
        .all()
    )
    return {"logs": [log.to_dict() for log in logs]}
