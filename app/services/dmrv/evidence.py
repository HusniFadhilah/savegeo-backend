"""Deterministic evidence-package generation and checksum verification.

Packages contain only artifacts already present in an immutable dMRV snapshot.
Missing methodology, monitoring, baseline, accounting, uncertainty, QA/QC and
report artifacts are listed as blockers; the service never manufactures them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_job import DmrvSnapshot
from app.services.dmrv.audit import record_audit
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError

REQUIRED_COMPONENTS = (
    "methodology_mapping_and_approval",
    "monitoring_run_scene_and_model_provenance",
    "baseline_or_documented_not_applicable_decision",
    "accounting_ledger_and_input_snapshot",
    "uncertainty_and_leakage_assessment",
    "field_qa_qc_and_data_chain_of_custody",
    "spatial_artifacts_and_quality_report",
    "reproducible_report_and_distribution_licences",
)
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def evidence_root() -> Path:
    configured = os.environ.get("DMRV_EVIDENCE_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[3] / "var" / "dmrv" / "evidence"


def _component_status(snapshot: DmrvSnapshot, component: str) -> str:
    """Require explicit artifact keys; a generic field summary is never enough."""
    components = (snapshot.outputs or {}).get("evidence_components")
    if not isinstance(components, dict):
        return "missing"
    entry = components.get(component)
    if not isinstance(entry, dict) or entry.get("status") != "provided":
        return "missing"
    if not isinstance(entry.get("artifact"), (dict, list, str)):
        return "missing"
    return "provided"


def _zip_entry(name: str, contents: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _build_archive(snapshot: DmrvSnapshot) -> tuple[bytes, dict, list[str]]:
    inputs = snapshot.inputs or {}
    outputs = snapshot.outputs or {}
    payloads: dict[str, bytes] = {
        "snapshot/inputs.json": _canonical(inputs),
        "snapshot/outputs.json": _canonical(outputs),
        "snapshot/provenance.json": _canonical({
            "snapshot_id": snapshot.id,
            "snapshot_kind": snapshot.kind,
            "snapshot_hash": snapshot.snapshot_hash,
            "job_id": snapshot.job_id,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
            "created_by": snapshot.created_by,
        }),
        "README.txt": (
            "SaveGeo dMRV evidence export\n"
            "This package is a reproducibility aid, not a verified carbon-credit claim.\n"
            "Inspect manifest.json and verify all listed SHA-256 digests before use.\n"
            "Missing components are intentionally reported; do not infer zero from missing data.\n"
        ).encode("utf-8"),
    }
    carbon_mrv = outputs.get("carbon_mrv")
    if isinstance(carbon_mrv, dict):
        payloads["snapshot/carbon-mrv-status.json"] = _canonical(carbon_mrv)
    # Include optional snapshot artifacts under fixed, server-selected names.
    optional = {
        "field_measurements": "measurements.json",
        "accounting_ledger": "accounting/ledger.json",
        "scene_manifest": "monitoring/scenes.json",
        "baseline": "baseline/baseline.json",
        "qa_qc_report": "qa/qa_qc_report.json",
        "uncertainty_report": "uncertainty/report.json",
        "spatial_artifacts": "spatial/artifacts.json",
    }
    raw_components = outputs.get("evidence_components")
    components: dict[str, Any] = raw_components if isinstance(raw_components, dict) else {}
    for component, filename in optional.items():
        entry = components.get(component)
        if isinstance(entry, dict) and entry.get("status") == "provided" and isinstance(entry.get("artifact"), (dict, list, str)):
            payloads[filename] = _canonical(entry["artifact"])

    component_rows = [
        {"name": name, "status": _component_status(snapshot, name)}
        for name in REQUIRED_COMPONENTS
    ]
    # The compact current snapshots do not yet implement a complete monitoring,
    # baseline, accounting and quality pipeline. Keep this explicit and fail closed.
    missing = [row["name"] for row in component_rows if row["status"] != "provided"]
    entry_rows = [
        {"path": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(payloads.items())
    ]
    manifest = {
        "schema_version": "savegeo-dmrv-evidence-1",
        "project_id": snapshot.project_id,
        "snapshot": {"id": snapshot.id, "kind": snapshot.kind, "sha256": snapshot.snapshot_hash},
        "qualification": "reproducibility_package",
        "verification_status": "not_verified",
        "issuance_status": "not_issued",
        "carbon_mrv_status": ({
            "estimate": (carbon_mrv.get("estimate") or {}).get("status"),
            "calibration": (carbon_mrv.get("calibration") or {}).get("status"),
            "validation": (carbon_mrv.get("validation") or {}).get("status"),
            "validation_type": (carbon_mrv.get("validation") or {}).get("validation_type"),
            "segmentation": (carbon_mrv.get("segmentation") or {}).get("status"),
            "review": (carbon_mrv.get("review") or {}).get("status"),
            "accounting": (carbon_mrv.get("accounting") or {}).get("status"),
            "registry": (carbon_mrv.get("registry") or {}).get("status"),
        } if isinstance(carbon_mrv, dict) else None),
        "components": component_rows,
        "missing_components": missing,
        "entries": entry_rows,
        "limitations": [
            "Package contents do not establish methodology compliance or credit eligibility.",
            "A missing component is not a zero value or a not-applicable decision.",
            "External verification and issuance require source documents and authorized parties.",
        ],
    }
    manifest_bytes = _canonical(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(payloads.items()):
            archive.writestr(_zip_entry(name, data), data)
        archive.writestr(_zip_entry("manifest.json", manifest_bytes), manifest_bytes)
    archive_bytes = buffer.getvalue()
    if len(archive_bytes) > MAX_PACKAGE_BYTES:
        raise DmrvError(413, "evidence_package_too_large", "Paket bukti melebihi batas ukuran 64 MB.")
    return archive_bytes, manifest, missing


def create_package(
    db: Session,
    *,
    project_id: str,
    snapshot_id: str,
    principal_key: str,
    created_by: str,
    idempotency_key: str,
    request_hash: str,
    principal: Principal,
    request_id: str | None,
) -> tuple[DmrvEvidencePackage, bool]:
    existing = db.execute(
        select(DmrvEvidencePackage).where(
            DmrvEvidencePackage.principal_key == principal_key,
            DmrvEvidencePackage.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash or existing.project_id != project_id:
            raise DmrvError(409, "idempotency_key_reused", "Kunci idempotensi sudah digunakan untuk permintaan lain.")
        return existing, True

    snapshot = db.execute(
        select(DmrvSnapshot).where(DmrvSnapshot.id == snapshot_id, DmrvSnapshot.project_id == project_id)
    ).scalar_one_or_none()
    if snapshot is None:
        raise DmrvError(404, "not_found", "Snapshot tidak ditemukan.")
    archive_bytes, manifest, missing = _build_archive(snapshot)
    package_id = str(uuid.uuid4())
    storage_key = f"{package_id}.zip"
    root = evidence_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / storage_key
    if target.parent != root:
        raise DmrvError(500, "evidence_storage_error", "Lokasi penyimpanan paket bukti tidak aman.")
    file_hash = hashlib.sha256(archive_bytes).hexdigest()
    manifest_hash = hashlib.sha256(_canonical(manifest)).hexdigest()
    row = DmrvEvidencePackage(
        id=package_id,
        project_id=project_id,
        snapshot_id=snapshot_id,
        principal_key=principal_key,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        storage_key=storage_key,
        manifest=manifest,
        manifest_hash=manifest_hash,
        package_sha256=file_hash,
        missing_components=missing,
        readiness="ready" if not missing else "incomplete",
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    fd, temp_name = tempfile.mkstemp(prefix=f".{package_id}.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(archive_bytes)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, target)
        record_audit(
            db,
            principal=principal,
            action="evidence.package.create",
            resource_type="dmrv_evidence_package",
            resource_id=package_id,
            project_id=project_id,
            after={"snapshot_id": snapshot_id, "readiness": row.readiness, "package_sha256": file_hash},
            snapshot_hash=snapshot.snapshot_hash,
            request_id=request_id,
        )
        db.commit()
    except Exception:
        db.rollback()
        try:
            Path(temp_name).unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    db.refresh(row)
    return row, False


def package_path(row: DmrvEvidencePackage) -> Path:
    root = evidence_root()
    # storage_key is generated internally and still validated on every read.
    if Path(row.storage_key).name != row.storage_key or not row.storage_key.endswith(".zip"):
        raise DmrvError(500, "evidence_storage_error", "Referensi paket bukti tidak valid.")
    path = (root / row.storage_key).resolve()
    if path.parent != root:
        raise DmrvError(500, "evidence_storage_error", "Lokasi paket bukti tidak valid.")
    return path


def verify_package(row: DmrvEvidencePackage) -> dict:
    path = package_path(row)
    if not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
        return {"valid": False, "code": "package_missing_or_too_large", "checked_entries": 0}
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row.package_sha256:
        return {"valid": False, "code": "package_hash_mismatch", "checked_entries": 0}
    if hashlib.sha256(_canonical(row.manifest)).hexdigest() != row.manifest_hash:
        return {"valid": False, "code": "manifest_hash_mismatch", "checked_entries": 0}
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or any(name.startswith("/") or ".." in Path(name).parts for name in names):
                return {"valid": False, "code": "unsafe_archive_paths", "checked_entries": 0}
            if sum(item.file_size for item in infos) > MAX_PACKAGE_BYTES:
                return {"valid": False, "code": "expanded_size_limit", "checked_entries": 0}
            manifest_bytes = archive.read("manifest.json")
            parsed = json.loads(manifest_bytes)
            if parsed != row.manifest:
                return {"valid": False, "code": "manifest_content_mismatch", "checked_entries": 0}
            for entry in row.manifest.get("entries", []):
                data = archive.read(entry["path"])
                if len(data) != entry["size_bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    return {"valid": False, "code": "entry_hash_mismatch", "checked_entries": 0}
            expected = {item["path"] for item in row.manifest.get("entries", [])} | {"manifest.json"}
            if set(names) != expected:
                return {"valid": False, "code": "unexpected_archive_entries", "checked_entries": 0}
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, json.JSONDecodeError):
        return {"valid": False, "code": "invalid_archive", "checked_entries": 0}
    return {"valid": True, "code": None, "checked_entries": len(row.manifest.get("entries", []))}
