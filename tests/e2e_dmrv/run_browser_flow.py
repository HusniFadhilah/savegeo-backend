"""Real-browser (Chromium) end-to-end flow for dMRV against the live throwaway stack."""
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parent
SHOTS = ROOT / "shots"  # git-ignored output
SHOTS.mkdir(exist_ok=True)
APP = "http://localhost:5501"
API = "http://127.0.0.1:8086/api"
PASSWORD = "E2e-Passw0rd!"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "dmrv"
RUN = str(int(time.time()))
NAME = f"Browser Pilot {RUN}"
LON = round(113.0 + random.random() * 3, 4)  # unique per run: no accidental overlap with earlier runs
FAR_POLYGON = {"type": "Polygon", "coordinates": [[[LON, -7.5], [LON + 0.01, -7.5], [LON + 0.01, -7.49], [LON, -7.49], [LON, -7.5]]]}
BOWTIE = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
results, console_errors = [], []


def check(name, condition, detail=""):
    results.append({"case": name, "passed": bool(condition), "detail": "" if condition else detail})
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))


def watch(page, label):
    def on_console(msg):
        text = msg.text
        if msg.type == "error" and not re.search(r"tile|basemap|favicon|ERR_|Failed to load resource|net::|earth|cesium|WebGL|maps?\.|gibs|mapkit", text, re.I):
            console_errors.append(f"[{label}] {text[:200]}")
    page.on("console", on_console)
    page.on("pageerror", lambda err: console_errors.append(f"[{label}] pageerror: {str(err)[:200]}"))


def login(page, username):
    page.goto(f"{APP}/login", wait_until="networkidle")
    page.fill("#unified-username", username)
    page.fill("#unified-password", PASSWORD)
    page.get_by_role("button", name="Login", exact=True).click()
    page.wait_for_url(lambda url: "/login" not in url, timeout=30000)


def shot(page, name):
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---------------- owner (admin): full flow
    ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="id-ID")
    page = ctx.new_page()
    watch(page, "owner")
    login(page, "owner-a")
    page.goto(f"{APP}/dmrv/projects", wait_until="networkidle")
    expect(page.get_by_role("heading", name="Proyek dMRV")).to_be_visible(timeout=15000)
    check("projects page renders after real login", True)
    shot(page, "01_projects_list")

    page.get_by_label("Nama proyek").fill(NAME)
    page.get_by_label("Organisasi").fill("UNDIP")
    submit = page.get_by_role("button", name="Buat proyek")
    submit.dblclick()  # double click must create exactly one project
    expect(page.get_by_role("status").filter(has_text="Proyek dibuat.")).to_be_visible(timeout=15000)
    check("create project shows success status", True)
    link = page.get_by_role("link", name=NAME)
    check("exactly one project created on double click", link.count() == 1, f"links={link.count()}")
    shot(page, "02_project_created")

    link.first.click()
    expect(page.get_by_role("heading", name=NAME)).to_be_visible(timeout=15000)
    project_id = re.search(r"/dmrv/projects/([0-9a-f-]{36})", page.url).group(1)
    check("workspace shows role Pemilik proyek", page.get_by_text("Pemilik proyek").count() >= 1)
    shot(page, "03_workspace_overview")

    # boundary: invalid then valid
    page.get_by_role("tab", name="Batas wilayah").click()
    expect(page.get_by_text("Belum ada batas wilayah")).to_be_visible()
    shot(page, "04_boundary_empty")
    page.get_by_label("GeoJSON (Polygon atau MultiPolygon)").fill(json.dumps(BOWTIE))
    page.get_by_label("Alasan versi ini").fill("uji bowtie")
    page.get_by_role("button", name="Simpan versi baru").click()
    expect(page.get_by_role("alert").filter(has_text="invalid_geometry")).to_be_visible(timeout=15000)
    check("server validation error code shown for bowtie", True)
    shot(page, "05_boundary_invalid")
    page.get_by_label("GeoJSON (Polygon atau MultiPolygon)").fill(json.dumps(FAR_POLYGON))
    page.get_by_label("Alasan versi ini").fill("batas awal")
    page.get_by_role("button", name="Simpan versi baru").click()
    expect(page.get_by_role("status").filter(has_text="Versi 1 disimpan.")).to_be_visible(timeout=15000)
    check("valid boundary saved as version 1 with no-overlap message", page.get_by_role("status").inner_text().count("Tidak ada tumpang tindih") == 1)
    rows = page.locator("table tbody tr")
    expect(rows).to_have_count(1, timeout=15000)  # list refresh follows the status message
    body_text = page.locator("table tbody").first.inner_text()
    check("boundary row lists area ~122 ha", rows.count() == 1 and re.search(r"12[0-3],\d\d", body_text) is not None, f"rows={rows.count()} tbody_count={page.locator('table tbody').count()} text={body_text!r}")
    shot(page, "06_boundary_saved")

    # field data: preview -> import -> density
    page.get_by_role("tab", name="Data lapangan").click()
    expect(page.get_by_text("Belum ada data lapangan")).to_be_visible()
    import_btn = page.get_by_role("button", name="Impor")
    check("import locked before preview", import_btn.is_disabled())
    page.get_by_label("Berkas CSV atau XLSX").set_input_files(str(FIX / "bu_dessy_sample.csv"))
    page.get_by_label("Satuan kolom tinggi").select_option("cm")
    page.get_by_label("Luas plot (m²)").fill("400")
    page.get_by_role("button", name="Pratinjau").click()
    expect(page.get_by_text("21 baris, 0 galat, 2 peringatan")).to_be_visible(timeout=20000)
    check("preview shows 21 rows, 0 errors, 2 real data-quality warnings and the column mapping", page.get_by_text("Point ID").count() >= 1)
    check("source-data inconsistency flagged with row number (dbh vs circumference)", page.get_by_role("table", name="Masalah per baris").inner_text().count("dbh_circumference_mismatch") == 2)
    check("import enabled after preview", import_btn.is_enabled())
    shot(page, "07_field_preview")
    import_btn.click()
    expect(page.get_by_role("status").filter(has_text="Hasil impor: berhasil; 21 baris diimpor")).to_be_visible(timeout=30000)
    check("import committed 21 rows", True)
    check("import locked again after a finished import", page.get_by_role("button", name="Impor").is_disabled())
    expect(page.get_by_role("cell", name="1", exact=True).first).to_be_visible()
    page.get_by_role("button", name="Lihat kunjungan").first.click()
    expect(page.get_by_text("Mg C/ha")).to_be_visible(timeout=15000)
    density_text = page.get_by_text(re.compile(r"\d+,\d\d Mg C/ha")).first.inner_text()
    check("density shown with unit", "Mg C/ha" in density_text, density_text)
    check("area basis 'Asumsi' shown beside the density", page.get_by_text("dasar luas: Asumsi").count() == 1)
    check("year-only visit labelled", page.get_by_text("2024 (hanya tahun)").count() == 1)
    shot(page, "08_field_density")

    # second import of same visit: atomic reject, nothing duplicated
    page.get_by_label("Berkas CSV atau XLSX").set_input_files(str(FIX / "bu_dessy_sample.csv"))
    page.get_by_role("button", name="Pratinjau").click()
    expect(page.get_by_text(re.compile(r"21 baris, 21 galat"))).to_be_visible(timeout=20000)
    check("preview flags tree_exists for every row", page.get_by_role("table", name="Masalah per baris").inner_text().count("tree_exists") == 21)
    shot(page, "09_field_tree_exists")

    # English
    page.evaluate("localStorage.setItem('savegeo_language','en')")
    page.goto(f"{APP}/dmrv/projects", wait_until="networkidle")
    check("English heading after language switch", page.get_by_role("heading", name="dMRV projects").is_visible())
    shot(page, "10_projects_en")

    # mobile layout
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{APP}/dmrv/projects/{project_id}", wait_until="networkidle")
    page.get_by_role("tab", name="Field data").click()
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check("no horizontal page scroll at 390px width", overflow <= 1, f"overflow={overflow}px")
    shot(page, "11_workspace_mobile")
    ctx.close()

    # ---------------- stranger (user, non-member)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="id-ID")
    page = ctx.new_page()
    watch(page, "stranger")
    login(page, "stranger")
    page.goto(f"{APP}/dmrv/projects/{project_id}", wait_until="networkidle")
    expect(page.get_by_role("alert")).to_contain_text("Proyek tidak ditemukan, atau Anda bukan anggotanya.", timeout=15000)
    check("non-member sees neutral not-found (no project data)", page.get_by_text(NAME).count() == 0)
    shot(page, "12_non_member_not_found")
    page.goto(f"{APP}/dmrv/projects", wait_until="networkidle")
    expect(page.get_by_text("Belum ada proyek")).to_be_visible(timeout=15000)
    check("non-member list is empty with next action", True)
    ctx.close()

    # ---------------- field operator (user) after membership
    owner = requests.Session()
    owner.headers.update({"Origin": "http://localhost:5501"})
    owner.post(f"{API}/admin/auth/login", json={"username": "owner-a", "password": PASSWORD}).raise_for_status()
    r = owner.post(f"{API}/dmrv/projects/{project_id}/members", json={"principal_type": "user", "username": "field-user", "role": "field_operator"})
    check("owner adds field operator via API", r.status_code == 201, r.text[:120])
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="id-ID")
    page = ctx.new_page()
    watch(page, "operator")
    login(page, "field-user")
    page.goto(f"{APP}/dmrv/projects/{project_id}", wait_until="networkidle")
    expect(page.get_by_text("Operator lapangan").first).to_be_visible(timeout=15000)
    page.get_by_role("tab", name="Batas wilayah").click()
    expect(page.get_by_text("Peran Anda hanya dapat melihat batas wilayah.")).to_be_visible()
    check("operator: boundary form hidden (read-only)", page.get_by_label("Alasan versi ini").count() == 0)
    shot(page, "13_operator_boundary_readonly")
    page.get_by_role("tab", name="Data lapangan").click()
    check("operator: field import form available", page.get_by_label("Berkas CSV atau XLSX").is_visible())
    shot(page, "14_operator_field")
    ctx.close()
    browser.close()

failed = [r["case"] for r in results if not r["passed"]]
check("no unexpected console/page errors on dMRV pages", not console_errors, "; ".join(console_errors[:5]))
meta = {
    "date": datetime.now().astimezone().isoformat(timespec="seconds"),
    "environment": "throwaway PostgreSQL 16 (127.0.0.1:55433/e2e) + uvicorn 127.0.0.1:8086 + Vite dev 127.0.0.1:5501, headless Chromium (Playwright), locale id-ID",
    "results": results,
    "console_errors": console_errors,
}
(ROOT / "browser_e2e_report.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\n{sum(r['passed'] for r in results)}/{len(results)} browser checks passed")
sys.exit(1 if any(not r["passed"] for r in results) else 0)
