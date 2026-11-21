"""Create an auditable, report-based UNDIP dMRV example.

The example preserves annual report summaries and their source conflicts as
draft integrity records. It deliberately creates no legal boundary, operational
monitoring period, synthetic field rows, or carbon-credit accounting result.
Run from ``backend``. The database is only changed when ``--apply`` is passed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.security import admin_has_permission
from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_integrity import DmrvIntegrityRecord
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.session import SessionLocal
from app.schemas.dmrv import ProjectCreate
from app.schemas.dmrv_integrity import EvidenceInput, RecordSave, RiskInput
from app.services.dmrv import integrity, projects
from app.services.dmrv.authz import Principal


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "examples" / "dmrv" / "undip_carbon_stock_report_series.json"
PROJECT_NAME = "CONTOH dMRV - UNDIP carbon stock 2022-2026 (rekonsiliasi data)"
PROJECT_CREATE_KEY = "seed-undip-carbon-demo-project-v1"
SOURCE_LINKS = [
    "https://sustainability.undip.ac.id/wp-content/uploads/2025/04/Laporan-Carbon-Stock-Undip-2023-Final-0304241.pdf",
    "https://sustainability.undip.ac.id/wp-content/uploads/2025/06/Laporan_Karbon_Offset-2024_comp.pdf",
    "https://drive.google.com/file/d/1yjT3k2Prt6zBr4536Eqz6oDfN1EZrNc9/view",
    "https://docs.google.com/spreadsheets/d/1Woq1AfB-qsEezr-6kmPhQv25beisfiv_/edit?gid=1711374899#gid=1711374899",
    "https://docs.google.com/spreadsheets/d/1ml2cFL62vuyLocQQ8HelvcZ-c50DRNNh/edit?rtpof=true",
]


def _load_report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _ensure_project(db, principal: Principal) -> DmrvProject:
    project = db.execute(
        select(DmrvProject).where(DmrvProject.name == PROJECT_NAME)
    ).scalar_one_or_none()
    if project is not None:
        membership = db.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.principal_type == principal.type,
                ProjectMember.principal_id == principal.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise RuntimeError(
                f"Nama contoh sudah dimiliki proyek lain ({project.id}); tidak membuat duplikat."
            )
        return project

    payload = ProjectCreate(
        name=PROJECT_NAME,
        organization="Universitas Diponegoro",
        activity_type="carbon_inventory",
        jurisdiction="Multi-lokasi UNDIP, Jawa Tengah",
        # The sources refer to different SNI versions and do not define an
        # approved dMRV accounting methodology, so do not preselect one here.
        methodology_id=None,
        methodology_version=None,
    )
    out, _ = projects.create_project(
        db, principal, payload, PROJECT_CREATE_KEY, request_id=None
    )
    project = db.get(DmrvProject, out.id)
    if project is None:
        raise RuntimeError("Proyek UNDIP tidak ditemukan setelah dibuat.")
    return project


def _ensure_record(
    db,
    principal: Principal,
    project_id: str,
    category: str,
    key: str,
    payload: EvidenceInput | RiskInput,
    title: str,
    status: str,
) -> DmrvIntegrityRecord:
    save = RecordSave(
        title=title,
        payload=payload.model_dump(mode="json"),
        status=status,
    )
    row = db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == category,
            DmrvIntegrityRecord.record_key == key,
        )
    ).scalar_one_or_none()
    if (
        row is not None
        and row.title == save.title
        and row.payload == save.payload
        and row.status == save.status
    ):
        return row

    integrity.save_record(
        db,
        principal,
        project_id,
        category,
        key,
        save,
        row.revision if row else 0,
    )
    return db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == category,
            DmrvIntegrityRecord.record_key == key,
        )
    ).scalar_one()


def _summaries(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    periods = report["periods"]
    lines = []
    for item in periods:
        year = item["measurement_year"]
        source_note = item["source_note"]
        if year == 2022:
            values = (
                f"AGB {item['agb_mg_c']:,.2f} Mg C; SOC belum dilaporkan; "
                f"luas vegetasi {item['vegetation_area_ha']:,.2f} ha."
            )
        elif year == 2024:
            values = (
                f"AGB {item['agb_mg_c']:,.2f} Mg C; "
                f"SOC {item['soc_mg_c']:,.2f} Mg C."
            )
        else:
            values = (
                "Nilai AGB dan SOC 2026 ditahan sebagai belum direkonsiliasi; "
                "lihat catatan risiko pada proyek."
            )
        lines.append(
            f"• Tahun data {year} (label laporan {item['report_label_year']}): "
            f"{values} {source_note} Sumber: {item['source']}"
        )
    period_summary = "\n".join(lines)
    url_list = "\n".join(SOURCE_LINKS)
    return [
        (
            "monitoring_plan",
            "Tahun data dan presisi tanggal",
            f"Tahun rujukan laporan yang tersedia: 2022, 2024, dan 2026. "
            f"Tidak ditemukan tanggal survei sampai hari/bulan pada dokumen yang diperiksa. "
            f"Tahun pada judul laporan tidak selalu sama dengan tahun pengukuran: laporan berlabel "
            f"2023 menyajikan data stok 2022. Belum dibuat periode monitoring operasional sampai "
            f"tanggal lapangan dan batas tiap lokasi dikonfirmasi.\n{period_summary}\nSumber tambahan:\n{url_list}",
        ),
        (
            "pool_selection",
            "Pool dan ringkasan stok dari laporan",
            f"Sumber menyediakan ringkasan AGB dan SOC untuk sebagian tahun; angka ini adalah "
            f"stok laporan, bukan hasil perubahan stok dMRV atau serapan bersih. BGB, litter, dan "
            f"deadwood tidak disimpulkan dari AGB/SOC dan tidak dimasukkan sebagai hasil terukur.\n"
            f"{period_summary}",
        ),
        (
            "field_protocol",
            "Protokol pengukuran dan tanggal lapangan",
            "Laporan 2023 menyebut acuan SNI 7724:2011; laporan 2024 menyebut SNI 7724:2019 "
            "dan menerangkan pengukuran biomassa serta SOC. Workbook berisi tabel transek, "
            "spesies/DBH, koordinat, dan perhitungan; workbook revisi juga mencantumkan rekonsiliasi "
            "sebagian koordinat. Tanggal kunjungan lapangan yang spesifik tidak dinyatakan pada "
            "sumber yang diperiksa, sehingga seed ini tidak membuat tanggal harian, pohon/plot baru, "
            "atau hasil validasi sintetis. Tautan sumber:\n" + url_list,
        ),
        (
            "methodology",
            "Metodologi masih perlu diselaraskan",
            "Sumber menggunakan acuan SNI dengan edisi berbeda (2011 dan 2019), dan belum "
            "menetapkan metodologi proyek dMRV, skenario baseline, additionality, leakage, "
            "permanence, atau aturan pengurangan ketidakpastian. Proyek ini karena itu tetap draf "
            "dan belum menampilkan perhitungan kredit.",
        ),
        (
            "baseline",
            "Inventaris tahunan bukan baseline kredit",
            "Perbandingan stok tahunan dari laporan UNDIP tidak dengan sendirinya menjadi baseline "
            "proyek atau estimasi perubahan stok yang dapat diklaim. Definisi area konsisten, "
            "frekuensi monitoring, kontrol perubahan batas, dan metode akuntansi masih perlu "
            "disepakati.",
        ),
        (
            "land_tenure",
            "Batas multi-lokasi dan hak pengelolaan",
            "Laporan mencakup beberapa lokasi kampus UNDIP dan menyebut total luas vegetasi. "
            "Dokumen yang diperiksa belum menyediakan geometri batas resmi tiap lokasi atau bukti "
            "hak/otorisasi pengelolaan untuk dMRV. Tidak ada poligon ilustratif yang dibuat atau "
            "disimpan oleh seed ini.",
        ),
        (
            "data_licences",
            "Sumber dan izin penggunaan data",
            "Dua laporan UNDIP tersedia pada situs Sustainability UNDIP. Workbook dan laporan 2026 "
            "berasal dari Google Drive yang aksesnya tidak dapat diasumsikan publik; pastikan hak "
            "akses dan izin penggunaan untuk anggota proyek sebelum mengunggahnya sebagai bukti.\n"
            + url_list,
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="admin", help="Username admin pemilik proyek")
    parser.add_argument(
        "--apply", action="store_true", help="Simpan proyek dan catatan draf ke database aktif"
    )
    args = parser.parse_args()
    report = _load_report()

    print(f"Contoh: {PROJECT_NAME}")
    print("Tahun data: 2022, 2024, 2026 (tanggal survei spesifik tidak tersedia).")
    print("Status: draf; tidak ada batas, periode operasional, atau hasil kredit sintetis.")
    print("Catatan mutu: angka AGB/SOC 2026 berbeda antarbagian laporan dan workbook.")
    if not args.apply:
        print("Pratinjau saja. Tambahkan --apply untuk menyimpan ke database.")
        return

    with SessionLocal() as db:
        admin = db.execute(
            select(AdminUser).where(AdminUser.username == args.owner)
        ).scalar_one_or_none()
        if admin is None or not admin.is_active:
            raise SystemExit(f"Akun admin aktif '{args.owner}' tidak ditemukan.")
        if not admin_has_permission(admin, "dmrv.project.create"):
            raise SystemExit(f"Akun '{args.owner}' tidak memiliki izin membuat proyek dMRV.")

        principal = Principal.from_account(admin)
        project = _ensure_project(db, principal)
        for key, title, summary in _summaries(report):
            payload = EvidenceInput(
                summary=summary,
                evidence=[],
                owner="Tim dMRV UNDIP · contoh berbasis laporan",
            )
            _ensure_record(
                db,
                principal,
                project.id,
                "readiness",
                key,
                payload,
                title,
                "draft",
            )

        risk_payload = RiskInput(
            summary=(
                "Rekonsiliasi angka laporan 2026 diperlukan sebelum nilai dipakai sebagai "
                "hasil atau baseline dMRV. PDF memuat AGB 23.712,25 (ringkasan), 23.712,35 "
                "(tabel), 23.721,25 (narasi), dan 22.658,85 Mg C (bagian lain); SOC tercatat "
                "1.729.329,77 dan 1.139.921,61 Mg C di bagian berbeda. Workbook revisi "
                "mencatat AGB 23.712,345 dan SOC 1.623.742,667 Mg C untuk 2026. Tahun pada "
                "sebagian tabel workbook juga tidak selaras dengan label laporan. Tidak satu pun "
                "angka konflik ini dimasukkan sebagai nilai akuntansi final. Sumber:\n"
                + "\n".join(SOURCE_LINKS)
            ),
            evidence=[],
            owner="Tim dMRV UNDIP · rekonsiliasi data",
            signal="data_gap",
            severity="high",
            observed_on=dt.date(2026, 11, 21),
            affected_area_ha=None,
        )
        _ensure_record(
            db,
            principal,
            project.id,
            "risk",
            "undip_report_value_reconciliation",
            risk_payload,
            "Rekonsiliasi stok karbon laporan 2026",
            "open",
        )

        print("Seed selesai:")
        print(f"  Project: /dmrv/projects/{project.id}")
        print("  Readiness: 7 catatan draf berbasis sumber")
        print("  Risk: 1 isu terbuka untuk rekonsiliasi angka")
        print("  Boundary/monitoring periods: belum dibuat; batas legal dan tanggal survei belum tersedia.")


if __name__ == "__main__":
    main()
