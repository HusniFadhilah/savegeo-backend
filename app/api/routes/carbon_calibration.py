"""Admin API for the auditable, plot-level PT Dahana calibration workflow."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import require_permission, get_current_app_viewer
from app.db.models.carbon_calibration import (
    CarbonCalibrationDataset,
    CarbonCalibrationFile,
    CarbonCalibrationModel,
    CarbonCalibrationRun,
    FieldPlot,
    FieldTree,
)
from app.db.session import SessionLocal, get_db
from app.schemas.carbon_calibration import CalibrationDatasetCreate, CalibrationRunCreate
from app.services import audit_service
from app.services.carbon_calibration_service import (
    CARBON_FRACTION,
    MAX_UPLOAD_BYTES,
    SUPPORTED_EXTENSIONS,
    build_plot_polygon,
    csv_rows,
    evaluate_predictions,
    file_kind,
    inspect_raster,
    lopo_linear_calibration,
    plot_carbon_density,
    reconcile_tree_record,
    safe_extract_zip,
    sha256_file,
    tree_carbon_from_dbh,
)
from app.services.carbon_workbook_service import parse_workbook, validate_raster_coverage

router = APIRouter(prefix="/admin/carbon-calibration", tags=["carbon-calibration"])
logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="carbon-calibration")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


def _job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(changes)


def _dataset_or_404(db: Session, dataset_id: str) -> CarbonCalibrationDataset:
    dataset = db.query(CarbonCalibrationDataset).filter_by(dataset_id=dataset_id).first()
    if not dataset:
        raise HTTPException(status_code=404, detail="Calibration dataset not found")
    return dataset


def _safe_filename(name: str) -> str:
    cleaned = Path(name or "upload.bin").name
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return cleaned[:255]


def _validate_magic(path: Path, suffix: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(8)
    if suffix == ".xlsx" and not header.startswith(b"PK"):
        raise HTTPException(status_code=415, detail="XLSX magic bytes are invalid")
    if suffix in {".tif", ".tiff"} and header[:4] not in {b"II*\x00", b"MM\x00*"}:
        raise HTTPException(status_code=415, detail="GeoTIFF magic bytes are invalid")


async def _store_upload(upload: UploadFile, dataset: CarbonCalibrationDataset) -> CarbonCalibrationFile:
    name = _safe_filename(upload.filename or "upload.bin")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415, detail=f"Unsupported calibration file extension: {suffix or 'none'}"
        )
    root = (get_settings().upload_path / "carbon-calibration" / dataset.dataset_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{uuid.uuid4().hex}_{name}"
    size = 0
    try:
        with destination.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Calibration file exceeds the 50 GB limit")
                output.write(chunk)
        _validate_magic(destination, suffix)
        digest, measured_size = sha256_file(destination)
        media_type = upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return CarbonCalibrationFile(
            dataset=dataset,
            source_name=name,
            stored_path=str(destination),
            media_type=media_type,
            size_bytes=measured_size,
            sha256=digest,
            file_kind=file_kind(name),
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _read_geojson_plots(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    features = payload.get("features", []) if payload.get("type") == "FeatureCollection" else [payload]
    plots = []
    for feature in features:
        properties = feature.get("properties") or {}
        plot_id = properties.get("plot_id") or properties.get("plot") or properties.get("id")
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "Polygon" or not coordinates or len(coordinates[0]) < 4 or not plot_id:
            continue
        result = build_plot_polygon(str(plot_id), coordinates[0][:-1], "EPSG:32748")
        plots.append({"result": result, "properties": properties})
    return plots


def _confirmed_plot_area(dataset: CarbonCalibrationDataset) -> tuple[float | None, float | None]:
    """Return plot area only when the manifest explicitly confirms it."""
    manifest = dataset.manifest or {}
    if not manifest.get("plot_area_confirmed"):
        return None, None
    area_m2 = manifest.get("plot_area_m2")
    try:
        area_m2 = float(area_m2)
    except (TypeError, ValueError):
        return None, None
    return (area_m2, area_m2 / 10000) if area_m2 > 0 else (None, None)


def _import_workbook_records(
    db: Session,
    dataset: CarbonCalibrationDataset,
    item: CarbonCalibrationFile,
    parsed: dict[str, Any],
) -> dict[str, Any]:
    source = parsed["source"]
    public_source = {key: value for key, value in source.items() if key != "path"}
    audit = parsed["audit"]
    source_sha = source["sha256"]
    already_imported = any(
        (plot.attributes or {}).get("source_sha256") == source_sha for plot in dataset.plots
    )
    item.metadata_json = {
        "status": "computed",
        "workbook_audit": audit,
        "sheets": parsed["sheets"],
        "import": {
            "status": "idempotent_replay_skipped" if already_imported else "imported",
            "source_sha256": source_sha,
            "carbon_pool": "agb_carbon",
            "formula_policy": "recalculate only whitelisted algebra; preserve raw Excel formula and cached value",
        },
    }
    if already_imported:
        return {"status": "idempotent_replay_skipped", **audit}

    area_m2, area_ha = _confirmed_plot_area(dataset)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in parsed["records"]:
        grouped.setdefault(record["plot_id"], []).append(record)
    version = f"workbook-{source_sha[:12]}"
    imported_plots = 0
    imported_trees = 0
    for plot_id, records in grouped.items():
        first = records[0]
        geometry = None
        if first["coordinate_status"] == "valid":
            geometry = {
                "type": "Point",
                "coordinates": [first["longitude"], first["latitude"]],
            }
        plot = FieldPlot(
            dataset_pk=dataset.id,
            plot_id=plot_id,
            geometry_original=geometry,
            geometry_normalized=geometry,
            crs="EPSG:4326",
            area_m2=area_m2,
            area_ha=area_ha,
            attributes={
                "source_sha256": source_sha,
                "source_workbook": source["filename"],
                "source_sheets": sorted({record["source_sheet"] for record in records}),
                "site_id": first["site_id"],
                "transect_id": first["transect_id"],
                "campaign_year": first["campaign_year"],
                "coordinate_status": first["coordinate_status"],
                "geometry_source": "workbook_dms_point; original coordinate preserved",
                "carbon_pool": "agb_carbon",
                "carbon_unit": "kg C per plot; density withheld until plot area is confirmed",
                "plot_area_source": "explicit_manifest" if area_m2 is not None else "not_declared",
            },
            qa_status="valid" if first["coordinate_status"] == "valid" else "review",
            source_document=source["filename"],
            source_table=first["source_sheet"],
            survey_date=str(first["campaign_year"]),
        )
        db.add(plot)
        db.flush()
        agb_kg = 0.0
        carbon_agb_kg = 0.0
        valid_carbon_count = 0
        for record in records:
            flags = record["validation_flags"]
            tree = FieldTree(
                plot_pk=plot.id,
                tree_id=str(record["source_row"]),
                species=record["scientific_name_normalized"] or record["species_raw"],
                circumference_cm=record["circumference_cm"],
                dbh_cm=record["dbh_cm"],
                agb_kg=record["agb_biomass_kg"],
                bgb_kg=None,
                carbon_agb_kg=record["agb_carbon_kg"],
                carbon_bgb_kg=None,
                formula_version=version,
                validation_status="review" if flags else "valid",
            )
            db.add(tree)
            imported_trees += 1
            if record["tree_status"] == "alive" and record["agb_carbon_kg"] is not None and not flags:
                agb_kg += record["agb_biomass_kg"] or 0
                carbon_agb_kg += record["agb_carbon_kg"] or 0
                valid_carbon_count += 1
        plot.attributes = {
            **(plot.attributes or {}),
            "tree_count": len(records),
            "valid_alive_tree_count": valid_carbon_count,
            "agb_biomass_kg_plot": agb_kg,
            "carbon_agb_kg_plot": carbon_agb_kg,
            "carbon_agb_mg_c_ha": plot_carbon_density(carbon_agb_kg, area_ha) if area_ha else None,
            "excluded_from_density_reason": None if area_ha else "plot_area_not_confirmed",
        }
        imported_plots += 1
    prior_report = dataset.validation_report or {}
    prior_audits = prior_report.get("workbook_audits", [])
    dataset.validation_report = {
        **prior_report,
        "workbook_audit": audit,
        "workbook_audits": [item for item in prior_audits if item.get("source_sha256") != source_sha]
        + [{"source_sha256": source_sha, "source_name": source["filename"], "audit": audit}],
        "source_sha256": source_sha,
        "carbon_pool": "agb_carbon",
        "imported_plots": int(prior_report.get("imported_plots", 0)) + imported_plots,
        "imported_trees": int(prior_report.get("imported_trees", 0)) + imported_trees,
    }
    prior_sources = (dataset.provenance or {}).get("workbook_sources", [])
    dataset.provenance = {
        **(dataset.provenance or {}),
        "workbook_sources": [item for item in prior_sources if item.get("sha256") != source_sha]
        + [public_source],
        "parser": "carbon_workbook_service.v1",
        "read_only_source": True,
    }
    return {"status": "imported", **audit, "imported_plots": imported_plots, "imported_trees": imported_trees}


def _extract_dataset(dataset_pk: int, job_id: str) -> None:
    db = SessionLocal()
    try:
        dataset = db.get(CarbonCalibrationDataset, dataset_pk)
        if not dataset:
            raise ValueError("Dataset not found")
        _job(job_id, status="running", progress=10)
        files = dataset.files
        for index, item in enumerate(files):
            path = Path(item.stored_path)
            if not path.exists():
                continue
            if item.file_kind == "archive":
                extract_dir = path.parent / f"extracted_{item.id}"
                item.metadata_json = {"members": safe_extract_zip(path, extract_dir)}
            elif item.file_kind == "raster":
                item.metadata_json = inspect_raster(path)
            elif path.suffix.lower() in {".geojson", ".json"}:
                try:
                    plots = _read_geojson_plots(path)
                    item.metadata_json = {"plot_count": len(plots)}
                    for plot in plots:
                        result = plot["result"]
                        if (
                            not db.query(FieldPlot)
                            .filter_by(
                                dataset_pk=dataset.id,
                                plot_id=plot["properties"].get("plot_id")
                                or result["original"]["properties"]["plot_id"],
                            )
                            .first()
                        ):
                            plot_id = result["original"]["properties"]["plot_id"]
                            db.add(
                                FieldPlot(
                                    dataset_pk=dataset.id,
                                    plot_id=plot_id,
                                    geometry_original=result["original"],
                                    geometry_normalized=result["normalized"],
                                    crs="EPSG:32748",
                                    area_m2=result["area_m2"],
                                    area_ha=result["area_m2"] / 10000,
                                    attributes=plot["properties"],
                                    qa_status="valid" if result["area_ok"] else "review",
                                    source_document=item.source_name,
                                )
                            )
                except (ValueError, json.JSONDecodeError) as exc:
                    item.metadata_json = {"status": "review", "error": str(exc)}
            elif path.suffix.lower() == ".csv":
                rows = csv_rows(path)
                item.metadata_json = {"row_count": len(rows), "columns": list(rows[0]) if rows else []}
                _import_tree_rows(db, dataset, rows, item.source_name)
            elif path.suffix.lower() == ".xlsx":
                source_name = item.source_name.lower()
                campaign_year = (
                    2024
                    if "survei carbon" in source_name or "previous" in source_name
                    else int((dataset.manifest or {}).get("survey_year", 2026))
                )
                parsed = parse_workbook(path, dataset.dataset_id, campaign_year)
                _import_workbook_records(db, dataset, item, parsed)
            _job(job_id, progress=10 + int((index + 1) / max(len(files), 1) * 80))
        dataset.status = "ready"
        dataset.provenance = {
            **(dataset.provenance or {}),
            "extracted_at": datetime.now(UTC).isoformat(),
            "processing": "streaming-file-ingestion",
        }
        db.commit()
        _job(job_id, status="complete", progress=100, result={"dataset_id": dataset.dataset_id})
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("Carbon calibration extraction failed")
        _job(job_id, status="failed", progress=100, error={"code": "extract_failed", "message": str(exc)})
    finally:
        db.close()


def _queue_dataset_job(dataset: CarbonCalibrationDataset, job_type: str) -> dict[str, str | int]:
    job_id = str(uuid.uuid4())
    _job(job_id, job_id=job_id, status="queued", progress=0, type=job_type, dataset_id=dataset.dataset_id)
    executor.submit(_extract_dataset, dataset.id, job_id)
    return {"job_id": job_id, "status": "queued", "status_url": f"/admin/carbon-calibration/jobs/{job_id}"}


def _spatial_report(dataset: CarbonCalibrationDataset, db: Session | None = None) -> dict[str, Any]:
    rasters = []
    for item in dataset.files:
        if item.file_kind != "raster":
            continue
        metadata = item.metadata_json or {}
        if metadata.get("status") != "computed":
            metadata = inspect_raster(Path(item.stored_path))
        if metadata.get("status") == "computed":
            rasters.append({"source_name": item.source_name, "metadata": metadata})
    if not rasters:
        return {
            "status": "not_computed",
            "blocking": True,
            "reason": "No readable DEM/DTM/DSM raster is attached",
        }
    plot_sources = [dataset]
    if db:
        for linked_id in (dataset.manifest or {}).get("linked_dataset_ids", []):
            linked = db.query(CarbonCalibrationDataset).filter_by(dataset_id=linked_id).first()
            if linked:
                plot_sources.append(linked)
    source_plots = [plot for source in plot_sources for plot in source.plots]
    if not source_plots:
        return {
            "status": "not_computed",
            "blocking": True,
            "reason": "No linked plot records are available for footprint validation",
            "rasters": rasters,
        }
    all_reports = []
    for raster in rasters:
        metadata = raster["metadata"]
        source_name = raster["source_name"].lower()
        plots = source_plots
        if "khdtk" in source_name:
            plots = [
                plot for plot in plots if "khdtk" in str((plot.attributes or {}).get("site_id", "")).lower()
            ]
        records = []
        for plot in plots:
            geometry = plot.geometry_original or {}
            coordinates = (geometry.get("coordinates") or []) if geometry.get("type") == "Point" else []
            lon, lat = (coordinates[0], coordinates[1]) if len(coordinates) >= 2 else (None, None)
            records.append(
                {
                    "plot_id": plot.plot_id,
                    "latitude": lat,
                    "longitude": lon,
                    "coordinate_status": (plot.attributes or {}).get("coordinate_status", "missing"),
                }
            )
        coverage = validate_raster_coverage(
            records,
            tuple(metadata["bounds"]),
            str(metadata.get("crs") or "unknown"),
        )
        all_reports.append({"source_name": raster["source_name"], "coverage": coverage})
    return {
        "status": "computed",
        "blocking": any(report["coverage"].get("blocking", True) for report in all_reports),
        "rasters": all_reports,
        "policy": "No coordinate shifting; terrain extraction requires every target plot to be covered",
    }


def _import_tree_rows(
    db: Session, dataset: CarbonCalibrationDataset, rows: list[dict[str, Any]], source_name: str
) -> None:
    for row in rows:
        plot_id = str(row.get("plot_id") or row.get("plot") or "").strip()
        if not plot_id:
            continue
        plot = db.query(FieldPlot).filter_by(dataset_pk=dataset.id, plot_id=plot_id).first()
        if not plot:
            plot = FieldPlot(
                dataset_pk=dataset.id,
                plot_id=plot_id,
                crs="EPSG:32748",
                area_m2=400,
                area_ha=0.04,
                attributes={},
                qa_status="review",
                source_document=source_name,
                source_table="CSV",
            )
            db.add(plot)
            db.flush()
        dbh = float(row.get("dbh_cm") or 0)
        calculated = tree_carbon_from_dbh(dbh)
        reconciliation = reconcile_tree_record(
            {**row, "dbh_cm": dbh, **{key: row.get(key) for key in calculated}}
        )
        tree = FieldTree(
            plot_pk=plot.id,
            tree_id=str(row.get("tree_id") or f"{plot_id}-{len(plot.trees) + 1}"),
            species=row.get("species") or "Hevea brasiliensis",
            circumference_cm=float(row["circumference_cm"]) if row.get("circumference_cm") else None,
            dbh_cm=dbh,
            **calculated,
            validation_status=reconciliation["status"],
        )
        db.add(tree)
        totals = {
            "agb_kg_plot": sum(item.agb_kg or 0 for item in plot.trees) + calculated["agb_kg"],
            "bgb_kg_plot": sum(item.bgb_kg or 0 for item in plot.trees) + calculated["bgb_kg"],
            "carbon_agb_kg_plot": sum(item.carbon_agb_kg or 0 for item in plot.trees)
            + calculated["carbon_agb_kg"],
            "carbon_bgb_kg_plot": sum(item.carbon_bgb_kg or 0 for item in plot.trees)
            + calculated["carbon_bgb_kg"],
        }
        totals["carbon_total_kg_plot"] = totals["carbon_agb_kg_plot"] + totals["carbon_bgb_kg_plot"]
        totals.update(
            {
                "carbon_agb_mg_c_ha": plot_carbon_density(totals["carbon_agb_kg_plot"]),
                "carbon_bgb_mg_c_ha": plot_carbon_density(totals["carbon_bgb_kg_plot"]),
                "carbon_total_mg_c_ha": plot_carbon_density(totals["carbon_total_kg_plot"]),
                "agb_kg_plot": totals["agb_kg_plot"],
                "bgb_kg_plot": totals["bgb_kg_plot"],
            }
        )
        plot.attributes = {
            **(plot.attributes or {}),
            **totals,
            "sampling_method": "purposive",
            "reference_type": "field-derived allometric reference",
        }


def _run_calibration(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(CarbonCalibrationRun, run_id)
        if not run:
            return
        dataset = db.get(CarbonCalibrationDataset, run.dataset_pk)
        run.status, run.progress = "running", 10
        db.commit()
        rows = []
        for plot in dataset.plots if dataset else []:
            attrs = plot.attributes or {}
            predicted = attrs.get("current_model_prediction")
            target_key = {
                "aboveground_biomass_carbon": "carbon_agb_mg_c_ha",
                "belowground_biomass_carbon": "carbon_bgb_mg_c_ha",
                "living_biomass_carbon": "carbon_total_mg_c_ha",
            }[run.target_pool]
            observed = attrs.get(target_key)
            if predicted is not None and observed is not None:
                rows.append(
                    {"plot_id": plot.plot_id, "predicted": float(predicted), "observed": float(observed)}
                )
        if run.method == "baseline":
            metrics = evaluate_predictions(rows)
        elif run.method == "linear_calibration":
            metrics = lopo_linear_calibration(rows)
        elif run.method == "intercept_bias":
            baseline = evaluate_predictions(rows)
            metrics = (
                {**baseline, "method": run.method, "bias_correction": -baseline.get("bias", 0)}
                if baseline.get("status") == "computed"
                else baseline
            )
        else:
            metrics = {
                "status": "not_computed",
                "reason": "Only baseline, intercept and LOPO linear calibration are production-supported for ten purposive plots",
            }
        run.metrics = {
            **metrics,
            "target_pool": run.target_pool,
            "independent_sample_unit": "plot",
            "sample_count": len(rows),
            "scientific_warning": "Purposive sampling is not full probability inference; BGB is model-derived from DBH.",
        }
        run.progress = 100
        run.status = "complete" if metrics.get("status") == "computed" else "not_computed"
        run.completed_at = datetime.now(UTC)
        if metrics.get("status") == "computed" and run.method == "linear_calibration":
            artifact_dir = get_settings().upload_path / "carbon-calibration" / dataset.dataset_id / "models"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            artifact_path = artifact_dir / f"{run.id}.json"
            artifact = {
                "schema_version": "carbon-calibration-model/v1",
                "method": "linear_calibration",
                "target_pool": run.target_pool,
                "target_unit": "Mg C/ha",
                "intercept": metrics.get("calibration_intercept"),
                "slope": metrics.get("calibration_slope"),
                "validation": metrics,
                "source_dataset_ids": [dataset.dataset_id],
                "source_dataset_checksums": [item.sha256 for item in dataset.files],
                "feature_schema": run.parameters.get("feature_schema", []),
                "uncertainty": {"status": "not_computed", "reason": "plot-level bootstrap not configured"},
                "scope": {
                    "site": "Ring 3 PT Dahana",
                    "location": "Subang, Jawa Barat",
                    "ecosystem": "rubber_plantation",
                    "species": ["Hevea brasiliensis"],
                    "survey_year": 2026,
                },
            }
            artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
            artifact_sha, _ = sha256_file(artifact_path)
            db.add(
                CarbonCalibrationModel(
                    model_id=f"{dataset.dataset_id}_{run.id}",
                    dataset_pk=dataset.id,
                    run_id=run.id,
                    model_name="PT Dahana LOPO Linear Calibration",
                    version="1.0.0",
                    target_pool=run.target_pool,
                    target_unit="Mg C/ha",
                    status="experimental",
                    metadata_json=artifact["scope"]
                    | {"validation_method": "leave_one_plot_out", "independent_sample_count": len(rows)},
                    artifact_path=str(artifact_path),
                    artifact_sha256=artifact_sha,
                    is_active=False,
                )
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(CarbonCalibrationRun, run_id)
        if run:
            run.status, run.error, run.progress = (
                "failed",
                {"code": "calibration_failed", "message": str(exc)},
                100,
            )
            db.commit()
    finally:
        db.close()


@router.post("/datasets", status_code=201, dependencies=[Depends(require_permission("calibration.write"))])
def create_dataset(
    payload: CalibrationDatasetCreate,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    if db.query(CarbonCalibrationDataset).filter_by(dataset_id=payload.dataset_id).first():
        raise HTTPException(status_code=409, detail="Dataset id already exists")
    dataset = CarbonCalibrationDataset(
        dataset_id=payload.dataset_id,
        name=payload.name,
        manifest=payload.manifest(),
        access_classification=payload.access,
        status=payload.status,
        is_active=False,
        owner_id=admin.id,
        provenance={
            "created_by": admin.username,
            "created_at": datetime.now(UTC).isoformat(),
            "source_policy": "uploaded-managed-storage-only",
        },
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    audit_service.log_audit(
        db,
        admin.id,
        "calibration.dataset.create",
        "carbon_calibration_dataset",
        str(dataset.id),
        {"dataset_id": dataset.dataset_id},
    )
    return dataset.to_dict()


@router.get("/datasets", dependencies=[Depends(require_permission("calibration.read"))])
def list_datasets(db: Session = Depends(get_db)):
    return {
        "datasets": [
            item.to_dict()
            for item in db.query(CarbonCalibrationDataset)
            .order_by(CarbonCalibrationDataset.created_at.desc())
            .all()
        ]
    }


@router.post(
    "/datasets/import", status_code=202, dependencies=[Depends(require_permission("calibration.write"))]
)
async def import_carbon_sources(
    revised_workbook: UploadFile = File(...),
    previous_workbook: UploadFile | None = File(None),
    dem: UploadFile | None = File(None),
    dataset_id: str = Form("bu_dessy_multilocation_carbon_2026"),
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    """Register each source as its own dataset and queue read-only extraction."""
    if Path(revised_workbook.filename or "").suffix.lower() != ".xlsx":
        raise HTTPException(status_code=415, detail="revised_workbook must be an XLSX file")
    if previous_workbook and Path(previous_workbook.filename or "").suffix.lower() != ".xlsx":
        raise HTTPException(status_code=415, detail="previous_workbook must be an XLSX file")
    if dem and Path(dem.filename or "").suffix.lower() not in {".tif", ".tiff"}:
        raise HTTPException(status_code=415, detail="dem must be a GeoTIFF file")

    base_id = dataset_id.removesuffix("_2026")
    definitions = [
        (
            f"{base_id}_2026",
            "Bu Dessy revised carbon survey 2026",
            revised_workbook,
            2026,
            "EPSG:4326",
            "agb_carbon",
        ),
    ]
    if previous_workbook:
        definitions.append(
            (
                f"{base_id}_2024",
                "Bu Dessy previous carbon survey 2024",
                previous_workbook,
                2024,
                "EPSG:4326",
                "agb_carbon",
            )
        )
    if dem:
        definitions.append(
            (
                "khdtk_high_resolution_dem",
                "KHDTK high-resolution elevation raster",
                dem,
                None,
                "EPSG:4326",
                "terrain",
            )
        )
    imported = []
    for source_id, name, upload, year, crs, carbon_pool in definitions:
        dataset = db.query(CarbonCalibrationDataset).filter_by(dataset_id=source_id).first()
        if not dataset:
            dataset = CarbonCalibrationDataset(
                dataset_id=source_id,
                name=name,
                manifest={
                    "site": "Multi-location field survey" if carbon_pool == "agb_carbon" else "KHDTK",
                    "location": "Jawa Tengah / KHDTK source workbook",
                    "crs": crs,
                    "survey_year": year,
                    "carbon_pool": carbon_pool,
                    "carbon_fraction": CARBON_FRACTION if carbon_pool == "agb_carbon" else None,
                    "plot_area_confirmed": False,
                    "plot_area_m2": None,
                    "linked_dataset_ids": [item[0] for item in definitions if item[5] == "agb_carbon"]
                    if carbon_pool == "terrain"
                    else [],
                    "target_pools": ["aboveground_biomass_carbon"] if carbon_pool == "agb_carbon" else [],
                },
                access_classification="restricted",
                status="draft",
                owner_id=admin.id,
                provenance={
                    "created_by": admin.username,
                    "created_at": datetime.now(UTC).isoformat(),
                    "source_policy": "uploaded-managed-storage-only",
                },
            )
            db.add(dataset)
            db.flush()
        stored = await _store_upload(upload, dataset)
        duplicate = (
            db.query(CarbonCalibrationFile).filter_by(dataset_pk=dataset.id, sha256=stored.sha256).first()
        )
        if duplicate:
            Path(stored.stored_path).unlink(missing_ok=True)
            stored = duplicate
        else:
            db.add(stored)
            db.flush()
        db.commit()
        db.refresh(dataset)
        audit_service.log_audit(
            db,
            admin.id,
            "calibration.sources.import",
            "carbon_calibration_dataset",
            str(dataset.id),
            {"dataset_id": dataset.dataset_id, "sha256": stored.sha256},
        )
        job = _queue_dataset_job(dataset, "import")
        imported.append({"dataset": dataset.to_dict(), "file": stored.to_dict(), **job})
    return {"datasets": imported}


@router.post(
    "/datasets/{dataset_id}/files",
    status_code=201,
    dependencies=[Depends(require_permission("calibration.write"))],
)
async def upload_dataset_file(
    dataset_id: str,
    file: UploadFile = File(...),
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = _dataset_or_404(db, dataset_id)
    stored = await _store_upload(file, dataset)
    duplicate = db.query(CarbonCalibrationFile).filter_by(dataset_pk=dataset.id, sha256=stored.sha256).first()
    if duplicate:
        Path(stored.stored_path).unlink(missing_ok=True)
        return duplicate.to_dict()
    db.add(stored)
    db.commit()
    db.refresh(stored)
    audit_service.log_audit(
        db,
        admin.id,
        "calibration.file.create",
        "carbon_calibration_file",
        str(stored.id),
        {"dataset_id": dataset_id, "sha256": stored.sha256},
    )
    return stored.to_dict()


@router.post(
    "/datasets/{dataset_id}/validate", dependencies=[Depends(require_permission("calibration.write"))]
)
def validate_dataset(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    errors = []
    allowed_crs = {"EPSG:32748", "EPSG:4326"}
    if dataset.manifest.get("crs") not in allowed_crs:
        errors.append(
            {
                "code": "crs_mismatch",
                "message": "Coordinates must declare EPSG:32748 projected or EPSG:4326 geographic CRS",
            }
        )
    if len(dataset.plots) and any(plot.crs not in allowed_crs for plot in dataset.plots):
        errors.append({"code": "plot_crs_mismatch", "message": "One or more plots have an unsupported CRS"})
    report = {
        "status": "review" if errors else "valid",
        "errors": errors,
        "file_count": len(dataset.files),
        "plot_count": len(dataset.plots),
        "independent_sample_count": len(dataset.plots),
        "warnings": [
            "Sampling is purposive; interval is indicative, not full-area probability inference",
            "Orthophoto RGB has no NIR and cannot produce NDVI/NDRE/SAVI",
            "Do not treat 90 trees as independent raster samples",
        ],
    }
    dataset.validation_report = report
    dataset.status = "validated" if not errors else "review"
    db.commit()
    return report


@router.post(
    "/datasets/{dataset_id}/extract",
    status_code=202,
    dependencies=[Depends(require_permission("calibration.write"))],
)
def extract_dataset(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    return _queue_dataset_job(dataset, "extract")


@router.post(
    "/datasets/{dataset_id}/reconcile",
    status_code=202,
    dependencies=[Depends(require_permission("calibration.write"))],
)
def reconcile_dataset(dataset_id: str, db: Session = Depends(get_db)):
    return _queue_dataset_job(_dataset_or_404(db, dataset_id), "reconcile")


@router.get("/jobs/{job_id}", dependencies=[Depends(require_permission("calibration.read"))])
def get_job(job_id: str):
    with jobs_lock:
        result = jobs.get(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Calibration job not found or expired")
    return result


@router.get("/datasets/{dataset_id}", dependencies=[Depends(require_permission("calibration.read"))])
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    return _dataset_or_404(db, dataset_id).to_dict(include_children=True)


@router.get("/datasets/{dataset_id}/audit", dependencies=[Depends(require_permission("calibration.read"))])
def get_dataset_audit(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    return {
        "dataset_id": dataset.dataset_id,
        "validation": dataset.validation_report,
        "provenance": dataset.provenance,
        "files": [
            {"source_name": item.source_name, "sha256": item.sha256, "metadata": item.metadata_json}
            for item in dataset.files
        ],
        "policy": {
            "source_read_only": True,
            "idempotency_key": "dataset_id + source_file_sha256",
            "training_unit": "plot, never individual tree",
            "carbon_pool": "AGB carbon only; BGB/SOC are not mixed",
        },
    }


@router.post(
    "/datasets/{dataset_id}/validate-spatial", dependencies=[Depends(require_permission("calibration.write"))]
)
def validate_spatial_dataset(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    report = _spatial_report(dataset, db)
    dataset.validation_report = {**(dataset.validation_report or {}), "spatial": report}
    if report.get("blocking"):
        dataset.status = "review"
    db.commit()
    return report


@router.post(
    "/datasets/{dataset_id}/extract-features", dependencies=[Depends(require_permission("calibration.write"))]
)
def extract_terrain_features(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    report = _spatial_report(dataset, db)
    if report.get("blocking"):
        raise HTTPException(status_code=409, detail={"code": "dem_coverage_blocked", **report})
    return {
        "status": "not_computed",
        "reason": "Terrain extraction requires a confirmed DTM/DSM type and a metric CRS; no feature values were fabricated",
        "spatial": report,
    }


@router.get("/datasets/{dataset_id}/plots", dependencies=[Depends(require_permission("calibration.read"))])
def get_plots(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": plot.geometry_normalized or plot.geometry_original,
                "properties": plot.to_dict(),
            }
            for plot in dataset.plots
        ],
    }


@router.get(
    "/datasets/{dataset_id}/quality-report", dependencies=[Depends(require_permission("calibration.read"))]
)
def get_quality_report(dataset_id: str, db: Session = Depends(get_db)):
    dataset = _dataset_or_404(db, dataset_id)
    return {
        "dataset_id": dataset.dataset_id,
        "validation": dataset.validation_report,
        "rasters": [
            {"source_name": item.source_name, "metadata": item.metadata_json or {"status": "not_computed"}}
            for item in dataset.files
            if item.file_kind == "raster"
        ],
        "plot_qa": {
            "total": len(dataset.plots),
            "area_ok": sum(
                1 for plot in dataset.plots if (plot.area_m2 or 0) and abs((plot.area_m2 or 0) - 400) <= 40
            ),
            "crs": sorted({plot.crs for plot in dataset.plots}) or [dataset.manifest.get("crs")],
            "area_not_confirmed": sum(1 for plot in dataset.plots if plot.area_m2 is None),
        },
        "scientific_limitations": [
            "CHM extreme heights need quality flag and robust percentiles; raw maximum is not used",
            "A 2026 local calibration is scoped to Ring 3 PT Dahana rubber plantation and cannot become the national default",
        ],
    }


@router.post("/runs", status_code=202, dependencies=[Depends(require_permission("calibration.write"))])
def create_run(
    payload: CalibrationRunCreate,
    admin=Depends(require_permission("calibration.write")),
    db: Session = Depends(get_db),
):
    dataset = _dataset_or_404(db, payload.dataset_id)
    run = CarbonCalibrationRun(
        id=str(uuid.uuid4()),
        dataset_pk=dataset.id,
        method=payload.method,
        target_pool=payload.target_pool,
        parameters=payload.parameters,
        created_by=admin.id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    executor.submit(_run_calibration, run.id)
    return run.to_dict()


@router.get("/runs/{run_id}", dependencies=[Depends(require_permission("calibration.read"))])
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(CarbonCalibrationRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Calibration run not found")
    return run.to_dict()


@router.post("/models/{model_id}/activate", dependencies=[Depends(require_permission("calibration.write"))])
def activate_model(
    model_id: int, admin=Depends(require_permission("calibration.write")), db: Session = Depends(get_db)
):
    model = db.get(CarbonCalibrationModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Calibration model not found")
    if model.status not in {"approved_local", "active_local"}:
        raise HTTPException(status_code=409, detail="Model must be approved before activation")
    db.query(CarbonCalibrationModel).filter(CarbonCalibrationModel.dataset_pk == model.dataset_pk).update(
        {CarbonCalibrationModel.is_active: False}
    )
    model.is_active, model.status = True, "active_local"
    db.commit()
    audit_service.log_audit(
        db,
        admin.id,
        "calibration.model.activate",
        "carbon_calibration_model",
        str(model.id),
        {"scope": "PT Dahana Subang only"},
    )
    return model.to_dict()


@router.post("/runs/{run_id}/approve", dependencies=[Depends(require_permission("calibration.write"))])
def approve_run(
    run_id: str, admin=Depends(require_permission("calibration.write")), db: Session = Depends(get_db)
):
    run = db.get(CarbonCalibrationRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Calibration run not found")
    model = db.query(CarbonCalibrationModel).filter_by(run_id=run_id).first()
    if not model or run.status != "complete":
        raise HTTPException(status_code=409, detail="A completed run with a calibration artifact is required")
    model.status = "approved_local"
    model.metadata_json = {
        **(model.metadata_json or {}),
        "approved_at": datetime.now(UTC).isoformat(),
        "approved_by": admin.username,
    }
    db.commit()
    audit_service.log_audit(
        db,
        admin.id,
        "calibration.model.approve",
        "carbon_calibration_model",
        str(model.id),
        {"run_id": run_id},
    )
    return {"run": run.to_dict(), "model": model.to_dict()}


@router.post("/models/{model_id}/promote", dependencies=[Depends(require_permission("calibration.write"))])
def promote_model(
    model_id: int, admin=Depends(require_permission("calibration.write")), db: Session = Depends(get_db)
):
    """Promote a validated artifact to approved-local; activation stays explicit."""
    model = db.get(CarbonCalibrationModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Calibration model not found")
    run = db.get(CarbonCalibrationRun, model.run_id) if model.run_id else None
    if model.status not in {"experimental", "approved_local"} or not run or run.status != "complete":
        raise HTTPException(status_code=409, detail="A completed validation run is required before promotion")
    metrics = run.metrics or {}
    report = db.get(CarbonCalibrationDataset, model.dataset_pk).validation_report or {}
    blockers = []
    if metrics.get("status") != "computed":
        blockers.append("validation_metrics_not_computed")
    if int(metrics.get("sample_count", metrics.get("n_plots", 0))) < 3:
        blockers.append("minimum_plot_count_not_met")
    if report.get("spatial", {}).get("blocking"):
        blockers.append("spatial_validation_blocked")
    for audit in report.get("workbook_audits", []):
        if (
            audit.get("audit", {}).get("tree_review_count", 0)
            and model.target_pool == "aboveground_biomass_carbon"
        ):
            blockers.append("unresolved_or_reviewed_label_records")
    if blockers:
        raise HTTPException(
            status_code=409, detail={"code": "promotion_guard", "blockers": sorted(set(blockers))}
        )
    model.status = "approved_local"
    model.metadata_json = {
        **(model.metadata_json or {}),
        "promoted_at": datetime.now(UTC).isoformat(),
        "promoted_by": admin.username,
        "activation_policy": "explicit local activation only; national default unchanged",
    }
    db.commit()
    audit_service.log_audit(
        db,
        admin.id,
        "calibration.model.promote",
        "carbon_calibration_model",
        str(model.id),
        {"scope": "PT Dahana Subang only"},
    )
    return model.to_dict()


@router.post("/models/{model_id}/rollback", dependencies=[Depends(require_permission("calibration.write"))])
def rollback_model(
    model_id: int, admin=Depends(require_permission("calibration.write")), db: Session = Depends(get_db)
):
    model = db.get(CarbonCalibrationModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Calibration model not found")
    model.is_active, model.status = False, "archived"
    db.commit()
    audit_service.log_audit(
        db, admin.id, "calibration.model.rollback", "carbon_calibration_model", str(model.id)
    )
    return model.to_dict()


@router.get("/models/{model_id}/validation", dependencies=[Depends(require_permission("calibration.read"))])
def get_model_validation(model_id: int, db: Session = Depends(get_db)):
    model = db.get(CarbonCalibrationModel, model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Calibration model not found")
    run = db.get(CarbonCalibrationRun, model.run_id) if model.run_id else None
    return {
        "model": model.to_dict(),
        "metrics": run.metrics if run else None,
        "validation": "leave_one_plot_out",
        "status": "not_computed" if not run or not run.metrics else run.metrics.get("status"),
    }


public_router = APIRouter(prefix="/carbon", tags=["carbon-calibration"])


@public_router.get("/calibration-models")
def list_public_calibration_models(
    aoi: str | None = None, _viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    models = (
        db.query(CarbonCalibrationModel)
        .filter(CarbonCalibrationModel.is_active.is_(True), CarbonCalibrationModel.status == "active_local")
        .all()
    )
    return {
        "models": [model.to_dict() for model in models],
        "selection": "explicit",
        "warning": "Local calibration is scoped to PT Dahana Subang rubber plantation; it is not the national default.",
        "aoi_received": bool(aoi),
    }


@public_router.get("/models/{model_id}/validation")
def public_model_validation(
    model_id: int, _viewer=Depends(get_current_app_viewer), db: Session = Depends(get_db)
):
    return get_model_validation(model_id, db)
