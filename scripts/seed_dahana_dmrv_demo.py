"""Seed an auditable dMRV example based on the PT Dahana field-survey report.

This command is explicit and idempotent; it never runs during application startup.
It imports the report's tree-level AGB data and marks every generated value as a
simulation. Run it from ``backend`` with ``--apply`` against the intended DB.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any

from pyproj import Transformer
from sqlalchemy import select

from app.core.security import admin_has_permission
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_field import SurveyPlot
from app.db.models.dmrv_integrity import DmrvIntegrityRecord
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.session import SessionLocal
from app.schemas.dmrv import (
    MonitoringPeriodCreate,
    MonitoringPeriodUpdate,
    ProjectCreate,
    SubmissionCreate,
)
from app.schemas.dmrv_integrity import (
    AccountingInput,
    EvidenceInput,
    PoolStock,
    RecordSave,
    RiskInput,
    ValidationInput,
)
from app.services.dmrv import evidence as evidence_service
from app.services.dmrv import field_data, integrity, jobs, monitoring, projects, submissions
from app.services.dmrv.authz import Principal
from app.services.dmrv.audit import record_audit
from app.services.dmrv.field_import import ImportOptions
from app.workers.dmrv_worker import run_one


ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "examples" / "dmrv" / "pt_dahana_subang_field_inventory.csv"
CSV_FILENAME = CSV_PATH.name
PROJECT_NAME = "CONTOH dMRV · PT Dahana Subang (data survei + simulasi)"
PROJECT_CREATE_KEY = "seed-dahana-demo-project-v1"
FIELD_IMPORT_KEY = "seed-dahana-demo-field-v1"
PERIOD_KEY = "seed-dahana-demo-period-v1"
SUMMARY_JOB_KEY = "seed-dahana-demo-summary-v1"
EVIDENCE_KEY = "seed-dahana-demo-evidence-v1"
SUBMISSION_KEY = "seed-dahana-demo-submission-v1"
DEMO_METHODOLOGY = "DEMO-ONLY"
DEMO_METHODOLOGY_VERSION = "0.1"

# Mean carbon stocks reported in Table 3.11, converted from kg C per 400 m² plot
# to Mg C/ha. Used as observations; predictions below are synthetic.
TRANSECT_CARBON_KG = {
    "AGB": [432.96, 971.08, 858.94, 621.74, 481.13, 682.54, 1378.87, 1729.19, 263.81, 1255.06],
    "BGB": [200.30, 416.07, 345.96, 247.23, 184.97, 267.62, 491.55, 646.90, 123.38, 452.89],
}
PREDICTION_FACTORS = {
    "AGB": [1.06, 0.96, 1.09, 0.92, 1.04, 0.95, 1.03, 0.91, 1.08, 0.97],
    "BGB": [0.95, 1.07, 0.93, 1.05, 0.97, 1.09, 0.91, 1.04, 0.96, 1.02],
}


def _read_plot_centres() -> dict[str, tuple[float, float]]:
    import csv

    centres: dict[str, tuple[float, float]] = {}
    with CSV_PATH.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            code = row["plot_code"]
            centres.setdefault(code, (float(row["longitude"]), float(row["latitude"])))
    expected = {str(index) for index in range(1, 11)}
    if set(centres) != expected:
        raise RuntimeError(f"Dataset harus berisi transek 1–10; ditemukan {sorted(centres)}")
    return centres


def _approximate_boundary(centres: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """Make a clearly labelled 300 × 300 m sample boundary around the plots."""
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32748", always_xy=True)
    to_wgs84 = Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)
    projected = [to_utm.transform(lon, lat) for lon, lat in centres.values()]
    centre_x = statistics.mean(point[0] for point in projected)
    centre_y = statistics.mean(point[1] for point in projected)
    half_size = 150.0
    corners = [
        (centre_x - half_size, centre_y - half_size),
        (centre_x + half_size, centre_y - half_size),
        (centre_x + half_size, centre_y + half_size),
        (centre_x - half_size, centre_y + half_size),
        (centre_x - half_size, centre_y - half_size),
    ]
    ring = [list(to_wgs84.transform(x, y)) for x, y in corners]
    return {"type": "Polygon", "coordinates": [ring]}


def _get_project(db, principal: Principal) -> DmrvProject:
    existing = db.execute(select(DmrvProject).where(DmrvProject.name == PROJECT_NAME)).scalar_one_or_none()
    if existing is not None:
        membership = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == existing.id,
                ProjectMember.principal_type == principal.type,
                ProjectMember.principal_id == principal.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise RuntimeError(
                f"Nama contoh sudah dipakai proyek lain ({existing.id}); tidak menambahkan duplikat."
            )
        return existing

    payload = ProjectCreate(
        name=PROJECT_NAME,
        organization="PT Dahana (Persero) · Ring 3",
        activity_type="carbon_inventory",
        jurisdiction="Cibogo, Kabupaten Subang, Jawa Barat",
        methodology_id=DEMO_METHODOLOGY,
        methodology_version=DEMO_METHODOLOGY_VERSION,
    )
    out, _ = projects.create_project(db, principal, payload, PROJECT_CREATE_KEY, request_id=None)
    project = db.get(DmrvProject, out.id)
    if project is None:
        raise RuntimeError("Proyek tidak ditemukan setelah dibuat.")
    return project


def _get_boundary(db, principal: Principal, project_id: str, centres: dict[str, tuple[float, float]]):
    row = db.execute(
        select(BoundaryVersion)
        .where(BoundaryVersion.project_id == project_id)
        .order_by(BoundaryVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return row
    out = projects.add_boundary(
        db,
        principal,
        project_id,
        _approximate_boundary(centres),
        "Contoh: kotak ilustratif 300 × 300 m berdasarkan luas laporan; bukan batas legal/final.",
        request_id=None,
    )
    return db.execute(
        select(BoundaryVersion).where(BoundaryVersion.id == out.id)
    ).scalar_one()


def _import_report_inventory(db, principal: Principal, project_id: str) -> dict[str, Any]:
    result = field_data.commit_import(
        db,
        principal,
        project_id,
        CSV_FILENAME,
        CSV_PATH.read_bytes(),
        mapping=None,
        options=ImportOptions(height_unit="m"),
        mode="atomic",
        request_id=None,
        idempotency_key=FIELD_IMPORT_KEY,
    )
    if result.get("status") != "committed" or result.get("rows_imported") != 90:
        raise RuntimeError(f"Impor data laporan tidak lengkap: {result}")
    return result


def _get_completed_period(db, principal: Principal, project_id: str, boundary_version: int):
    payload = MonitoringPeriodCreate(
        name="Survei lapangan 2026 · presisi tanggal tahunan",
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 12, 31),
        boundary_version=boundary_version,
    )
    period, _ = monitoring.create_period(
        db, principal, project_id, payload, PERIOD_KEY, request_id=None
    )
    row = db.get(DmrvMonitoringPeriod, period.id)
    if row is None:
        raise RuntimeError("Periode monitoring tidak ditemukan setelah dibuat.")
    if row.status == "planned":
        period = monitoring.update_period(
            db,
            principal,
            project_id,
            row.id,
            MonitoringPeriodUpdate(status="in_progress"),
            row.revision,
            request_id=None,
        )
        row = db.get(DmrvMonitoringPeriod, period.id)
    if row.status == "in_progress":
        monitoring.update_period(
            db,
            principal,
            project_id,
            row.id,
            MonitoringPeriodUpdate(status="completed"),
            row.revision,
            request_id=None,
        )
    db.expire_all()
    return db.get(DmrvMonitoringPeriod, period.id)


def _create_summary_snapshot(db, principal: Principal, project_id: str, period_id: str) -> DmrvSnapshot:
    job_payload = {"monitoring_period_id": period_id}
    request_body = {"kind": "field_summary", "payload": job_payload}
    request_hash = hashlib.sha256(
        json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = db.execute(
        select(DmrvJob).where(
            DmrvJob.project_id == project_id,
            DmrvJob.created_by == principal.key,
            DmrvJob.idempotency_key == SUMMARY_JOB_KEY,
        )
    ).scalar_one_or_none()
    if row is not None and row.request_hash != request_hash:
        raise RuntimeError("Idempotency key job contoh sudah digunakan dengan payload berbeda.")
    if row is None:
        queued_others = db.execute(
            select(DmrvJob.id).where(DmrvJob.status == "queued")
        ).scalars().all()
        if queued_others:
            raise RuntimeError(
                "Ada job lain dalam antrean; job demo tidak dijalankan agar tidak mengambil pekerjaan lain."
            )
        row = jobs.enqueue(
            db,
            project_id=project_id,
            kind="field_summary",
            payload=job_payload,
            created_by=principal.key,
            idempotency_key=SUMMARY_JOB_KEY,
            request_hash=request_hash,
        )
        record_audit(
            db,
            principal=principal,
            action="job.create",
            resource_type="dmrv_job",
            resource_id=row.id,
            project_id=project_id,
            after={"kind": row.kind, "status": row.status},
            request_id=None,
        )
        db.commit()

    if row.status == "queued":
        queued_others = db.execute(
            select(DmrvJob.id).where(DmrvJob.status == "queued", DmrvJob.id != row.id)
        ).scalars().all()
        if queued_others:
            raise RuntimeError(
                "Ada job lain dalam antrean; job demo tidak dijalankan agar tidak mengambil pekerjaan lain."
            )
        run_one(SessionLocal, "seed-dahana-demo-worker")
        db.expire_all()
        row = db.get(DmrvJob, row.id)
    if row is None or row.status != "succeeded":
        raise RuntimeError(f"Job ringkasan lapangan gagal: {getattr(row, 'error', None)}")
    snapshot_id = (row.result or {}).get("snapshot_id")
    snapshot = db.get(DmrvSnapshot, snapshot_id) if snapshot_id else None
    if snapshot is None:
        raise RuntimeError("Job ringkasan berhasil tetapi snapshot tidak ditemukan.")
    return snapshot


def _create_evidence_and_submission(db, principal: Principal, project_id: str, snapshot_id: str):
    body = {"project_id": project_id, "snapshot_id": snapshot_id}
    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    package, _ = evidence_service.create_package(
        db,
        project_id=project_id,
        snapshot_id=snapshot_id,
        principal_key=principal.key,
        created_by=principal.key,
        idempotency_key=EVIDENCE_KEY,
        request_hash=request_hash,
        principal=principal,
        request_id=None,
    )
    payload = SubmissionCreate(evidence_package_id=package.id)
    submission, _ = submissions.create_draft(
        db,
        project_id=project_id,
        principal=principal,
        payload=payload,
        idempotency_key=SUBMISSION_KEY,
        request_id=None,
    )
    return package, submission


def _ensure_record(
    db,
    principal: Principal,
    project_id: str,
    category: str,
    key: str,
    data: Any,
    title: str,
    status: str,
) -> DmrvIntegrityRecord:
    save = RecordSave(title=title, payload=data.model_dump(mode="json"), status=status)
    row = db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == category,
            DmrvIntegrityRecord.record_key == key,
        )
    ).scalar_one_or_none()
    if row is not None and row.title == save.title and row.payload == save.payload and row.status == save.status:
        return row
    expected_revision = row.revision if row else 0
    integrity.save_record(db, principal, project_id, category, key, save, expected_revision)
    return db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == category,
            DmrvIntegrityRecord.record_key == key,
        )
    ).scalar_one()


def _seed_integrity_examples(db, principal: Principal, project_id: str, period_id: str, area_ha: float, centres):
    provenance = (
        "Observasi per transek memakai ringkasan Tabel 3.11 Laporan Survei PT Dahana 2026. "
        "Prediksi di pasangan data dibuat secara deterministik hanya untuk demonstrasi antarmuka; "
        "bukan keluaran model, hasil kalibrasi, atau validasi independen. Sepuluh transek purposive "
        "berada dalam area berdekatan, sehingga dukungan validasi spasial tidak memadai."
    )
    for pool in ("AGB", "BGB"):
        pairs = []
        for index in range(10):
            plot_code = str(index + 1)
            lon, lat = centres[plot_code]
            observed = TRANSECT_CARBON_KG[pool][index] * 0.025
            predicted = observed * PREDICTION_FACTORS[pool][index]
            pairs.append(
                {
                    "point_id": f"T{index + 1}",
                    "lon": lon,
                    "lat": lat,
                    "observed": round(observed, 5),
                    "predicted": round(predicted, 5),
                }
            )
        payload = ValidationInput(
            pool=pool,
            product="SIMULASI DEMO · prediksi sintetis, bukan model operasional",
            reference_year=2026,
            predictor_year=2026,
            label_source="field_measurements",
            unit="Mg_C_ha",
            provenance=provenance,
            block_degrees=0.01,
            buffer_km=0,
            pairs=pairs,
            evidence=[],
        )
        _ensure_record(
            db,
            principal,
            project_id,
            "validation",
            f"dahana_demo_{pool.lower()}",
            payload,
            f"{pool} · DEMO prediksi sintetis vs ringkasan survei",
            "draft",
        )

    explanation = (
        "SIMULASI SAJA. Nilai akhir AGB dan BGB diambil dari rata-rata laporan survei PT Dahana. "
        "Nilai awal proyek dan baseline, serta seluruh nilai SOC, litter, dan deadwood, digenerate "
        "untuk menampilkan ledger. DEMO-ONLY bukan metodologi karbon terdaftar. Emisi proyek, "
        "leakage, pengurangan ketidakpastian, dan buffer sengaja kosong. Jangan gunakan hasil ini "
        "sebagai klaim, hasil validasi, atau unit kredit karbon."
    )
    accounting = AccountingInput(
        period_id=period_id,
        methodology_id=DEMO_METHODOLOGY,
        methodology_version=DEMO_METHODOLOGY_VERSION,
        profile_reference="https://example.org/savegeo-dmrv-demo-only",
        rule_justification=explanation,
        pools=[
            PoolStock(pool="AGB", project_start=20.90, project_end=21.69, baseline_start=20.90, baseline_end=21.00),
            PoolStock(pool="BGB", project_start=8.20, project_end=8.44, baseline_start=8.20, baseline_end=8.22),
            PoolStock(pool="SOC", project_start=72.40, project_end=72.70, baseline_start=72.40, baseline_end=72.45),
            PoolStock(pool="litter", project_start=1.80, project_end=1.90, baseline_start=1.80, baseline_end=1.82),
            PoolStock(pool="deadwood", project_start=3.70, project_end=3.50, baseline_start=3.70, baseline_end=3.68),
        ],
        excluded_pools_reason="",
        project_emissions_tco2e=None,
        leakage_tco2e=None,
        uncertainty_deduction_pct=None,
        buffer_pct=None,
        evidence=[],
    )
    _ensure_record(
        db,
        principal,
        project_id,
        "accounting",
        "dahana_demo_stock_change",
        accounting,
        "SIMULASI · perubahan stok lima pool (draft, non-kredit)",
        "draft",
    )

    readiness_records = [
        (
            "field_protocol",
            "Protokol lapangan dari laporan",
            "Laporan mencatat 10 transek 20 × 20 m dengan purposive sampling, pengukuran pohon DBH ≥10 cm, dan acuan SNI 7724:2019. Bukti laporan belum diunggah ke paket proyek.",
            "Tim survei · data bersumber dari laporan",
        ),
        (
            "baseline",
            "Baseline proyek untuk demo",
            "Laporan menyediakan satu rangkaian survei tahun 2026, bukan deret waktu baseline. Angka baseline di ledger adalah simulasi tampilan dan perlu diganti dengan data serta skenario yang disetujui.",
            "Tim dMRV · simulasi",
        ),
        (
            "pool_selection",
            "Cakupan pool pada contoh",
            "Laporan mengukur AGB dan menghitung BGB. SOC, litter, serta deadwood tidak diukur dalam laporan; angka ledger untuk tiga pool tersebut hanya simulasi.",
            "Tim dMRV · simulasi",
        ),
    ]
    for key, title, summary, owner in readiness_records:
        payload = EvidenceInput(summary=summary, evidence=[], owner=owner)
        _ensure_record(db, principal, project_id, "readiness", key, payload, title, "draft")

    risk = RiskInput(
        summary=(
            "DEMO · SOC, litter, dan deadwood tidak tercakup dalam pengukuran laporan. "
            "Nilai ledger untuk pool tersebut digenerate dan perlu diganti sebelum evaluasi nyata."
        ),
        evidence=[],
        due_date=dt.date(2026, 12, 31),
        owner="Tim dMRV · simulasi",
        signal="data_gap",
        severity="medium",
        observed_on=dt.date(2026, 11, 21),
        affected_area_ha=round(area_ha, 4),
    )
    _ensure_record(
        db,
        principal,
        project_id,
        "risk",
        "dahana_demo_missing_pools",
        risk,
        "DEMO · data lapangan pool SOC, litter, deadwood belum tersedia",
        "open",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="admin", help="Username akun admin pemilik proyek")
    parser.add_argument("--apply", action="store_true", help="Simpan contoh pada database yang dikonfigurasi")
    args = parser.parse_args()
    if not CSV_PATH.is_file():
        raise SystemExit(f"Dataset contoh tidak ditemukan: {CSV_PATH}")

    with SessionLocal() as db:
        admin = db.execute(select(AdminUser).where(AdminUser.username == args.owner)).scalar_one_or_none()
        if admin is None or not admin.is_active:
            raise SystemExit(f"Akun admin aktif '{args.owner}' tidak ditemukan.")
        if not admin_has_permission(admin, "dmrv.project.create"):
            raise SystemExit(f"Akun '{args.owner}' tidak memiliki izin membuat proyek dMRV.")
        project = db.execute(select(DmrvProject).where(DmrvProject.name == PROJECT_NAME)).scalar_one_or_none()
        preview_id = project.id if project else "dibuat saat --apply"
        print(f"Database seed target project: {PROJECT_NAME}")
        print(f"Owner: {args.owner} (admin:{admin.id}); project id: {preview_id}")
        print("Sumber lapangan: 90 pohon, 10 transek, AGB/BGB dari laporan PT Dahana 2026.")
        print("Batas 9 ha adalah geometri ilustratif; baseline, prediksi, SOC/litter/deadwood adalah simulasi.")
        if not args.apply:
            print("Belum ada perubahan. Tambahkan --apply untuk menyimpan.")
            return

        principal = Principal.from_account(admin)
        centres = _read_plot_centres()
        project = _get_project(db, principal)
        db_id = project.id
        boundary = _get_boundary(db, principal, db_id, centres)
        imported = _import_report_inventory(db, principal, db_id)
        period = _get_completed_period(db, principal, db_id, boundary.version)
        snapshot = _create_summary_snapshot(db, principal, db_id, period.id)
        package, submission = _create_evidence_and_submission(db, principal, db_id, snapshot.id)
        _seed_integrity_examples(db, principal, db_id, period.id, boundary.area_ha, centres)
        db.expire_all()

        plots = db.execute(select(SurveyPlot).where(SurveyPlot.project_id == db_id)).scalars().all()
        print("Seed selesai:")
        print(f"  Project: /dmrv/projects/{db_id}")
        print(f"  Boundary: sekitar {boundary.area_ha:.3f} ha (ilustratif)")
        print(f"  Field import: {imported['rows_imported']} pohon, {len(plots)} transek")
        print(f"  Monitoring period: {period.name} ({period.status})")
        print(f"  Snapshot: {snapshot.id}")
        print(f"  Evidence package: {package.readiness}; submission: {submission.status}")
        print("  Integrity: validasi demo AGB/BGB, ledger 5 pool, readiness, dan risk data gap.")


if __name__ == "__main__":
    main()
