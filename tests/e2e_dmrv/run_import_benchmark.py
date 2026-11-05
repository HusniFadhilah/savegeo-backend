"""Benchmark: large field-data imports against the live throwaway stack."""
import random
import sys
import time

import requests

BASE = "http://127.0.0.1:8086/api"
S = requests.Session()
S.headers.update({"Origin": "http://localhost:5501"})
S.post(f"{BASE}/admin/auth/login", json={"username": "owner-a", "password": "E2e-Passw0rd!"}).raise_for_status()
pid = S.post(f"{BASE}/dmrv/projects", json={"name": f"Bench {int(time.time())}"}, headers={"Idempotency-Key": f"bench-{time.time()}"}).json()["id"]


def make_csv(rows: int, plots: int, year: int, bad_every: int = 0) -> bytes:
    rng = random.Random(7)
    lines = ["plot_code,survey_year,tree_code,species,circumference_cm,dbh_cm,height,biomass_kg,latitude,longitude"]
    per_plot = rows // plots
    for plot in range(plots):
        lat, lon = -7 - plot * 0.0001, 110 + plot * 0.0001
        for tree in range(per_plot):
            dbh = round(rng.uniform(5, 80), 2)
            circ = round(dbh * 3.14159265, 1)
            bad = bad_every and (plot * per_plot + tree) % bad_every == 0
            lines.append(f"P{plot},{year},{tree},Sp{tree % 9},{'abc' if bad else circ},{dbh},{rng.randint(300, 2500)},{round(dbh ** 2 * 0.3, 2)},{lat:.6f},{lon:.6f}")
    return ("\n".join(lines) + "\n").encode()


def run(label, data, mode, key):
    form = {"height_unit": "cm", "plot_area_m2": "400", "area_basis": "assumed", "mode": mode}
    t0 = time.perf_counter()
    prev = S.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports/preview", files={"file": ("b.csv", data, "text/csv")}, data=form)
    t1 = time.perf_counter()
    com = S.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports", files={"file": ("b.csv", data, "text/csv")}, data=form, headers={"Idempotency-Key": key})
    t2 = time.perf_counter()
    body = com.json()
    print(f"{label}: size={len(data) / 1e6:.2f}MB preview={t1 - t0:.1f}s ({prev.status_code}, errors={prev.json()['report']['errors'] if prev.status_code == 200 else prev.text[:80]}) "
          f"commit={t2 - t1:.1f}s ({com.status_code}, {body.get('status')}, imported={body.get('rows_imported')}, rejected={body.get('rows_rejected')})")
    return prev, com, t1 - t0, t2 - t1


ok = True
_, c1, p1, t1 = run("20k rows/100 plots clean", make_csv(20000, 100, 2020), "atomic", "bench-a")
ok &= c1.status_code == 201 and c1.json()["rows_imported"] == 20000 and p1 < 30 and t1 < 60
_, c2, p2, t2 = run("50k rows/250 plots clean", make_csv(50000, 250, 2021), "atomic", "bench-b")
ok &= c2.status_code == 201 and c2.json()["rows_imported"] == 50000 and p2 < 60 and t2 < 120
_, c3, p3, t3 = run("20k rows with 1% bad rows, partial", make_csv(20000, 100, 2022, bad_every=100), "partial", "bench-c")
ok &= c3.json()["status"] == "partial" and c3.json()["rows_rejected"] == 200 and c3.json()["rows_imported"] == 19800
_, c4, p4, t4 = run("50001 rows must be rejected", make_csv(50001 - 0, 1, 2023)[:], "atomic", "bench-d")
ok &= c4.status_code == 422 and c4.json().get("code") == "too_many_rows"
plots = S.get(f"{BASE}/dmrv/projects/{pid}/field-data/plots")
print("plots listed:", len(plots.json()), "status", plots.status_code)
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
