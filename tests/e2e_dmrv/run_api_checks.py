"""Real-server API checks for dMRV (cookies, CSRF middleware, uniform 404, idempotency)."""
import sys
import random
import time
import uuid
from pathlib import Path

import requests

BASE = "http://127.0.0.1:8086/api"
ORIGIN = {"Origin": "http://localhost:5501"}
PASSWORD = "E2e-Passw0rd!"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "dmrv"

RUN = str(int(time.time()))
LON = round(114.0 + random.random() * 3, 4)  # unique location per run (no accidental overlap with earlier runs)


def square(lon, size=0.01):
    return {"type": "Polygon", "coordinates": [[[lon, -7.0], [lon + size, -7.0], [lon + size, -6.99], [lon, -6.99], [lon, -7.0]]]}


SQUARE, SHIFTED = square(LON), square(LON + 0.005)
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  [{detail}]" if detail and not condition else ""))


def session(kind, username):
    s = requests.Session()
    s.headers.update(ORIGIN)
    path = "/admin/auth/login" if kind == "admin" else "/auth/login"
    r = s.post(BASE + path, json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, (kind, username, r.status_code, r.text[:200])
    return s


owner_a, owner_b = session("admin", "owner-a"), session("admin", "owner-b")
field_user, stranger = session("user", "field-user"), session("user", "stranger")
anon = requests.Session()
anon.headers.update(ORIGIN)

# --- authentication boundary
check("anonymous list is 401", anon.get(BASE + "/dmrv/projects").status_code == 401)
check("user account cannot create a project (403)", field_user.post(BASE + "/dmrv/projects", json={"name": "x"}, headers={"Idempotency-Key": "u1-" + RUN}).status_code == 403)

# --- create (real CSRF middleware)
payload = {"name": "E2E Pilot " + RUN, "organization": "UNDIP", "methodology_id": "VM0047", "activity_type": "ARR"}
no_origin = requests.Session()
no_origin.cookies.update(owner_a.cookies)
r = no_origin.post(BASE + "/dmrv/projects", json=payload, headers={"Idempotency-Key": "csrf-1-" + RUN})
check("cookie POST without Origin/Referer is rejected by CSRF middleware", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
evil = requests.Session()
evil.cookies.update(owner_a.cookies)
r = evil.post(BASE + "/dmrv/projects", json=payload, headers={"Idempotency-Key": "csrf-2-" + RUN, "Origin": "https://evil.example"})
check("cookie POST from foreign Origin is rejected", r.status_code == 403, str(r.status_code))

r = owner_a.post(BASE + "/dmrv/projects", json=payload, headers={"Idempotency-Key": "e2e-create-1-" + RUN})
check("owner creates project (201)", r.status_code == 201, r.text[:200])
project = r.json()
pid = project["id"]
check("project owner role + ETag", project["my_role"] == "project_owner" and r.headers.get("ETag") == '"1"')
replay = owner_a.post(BASE + "/dmrv/projects", json=payload, headers={"Idempotency-Key": "e2e-create-1-" + RUN})
check("idempotent replay returns same project (200)", replay.status_code == 200 and replay.json()["id"] == pid)
check("X-Request-ID present on dMRV response", bool(r.headers.get("X-Request-ID")))

# --- isolation on the real stack
fake = str(uuid.uuid4())
for label, call in [
    ("GET", lambda s, p: s.get(f"{BASE}/dmrv/projects/{p}")),
    ("GET boundaries", lambda s, p: s.get(f"{BASE}/dmrv/projects/{p}/boundaries")),
    ("GET plots", lambda s, p: s.get(f"{BASE}/dmrv/projects/{p}/field-data/plots")),
    ("POST members", lambda s, p: s.post(f"{BASE}/dmrv/projects/{p}/members", json={"principal_type": "user", "username": "stranger", "role": "analyst"})),
]:
    a, b = call(owner_b, pid), call(owner_b, fake)
    strip = lambda x: {k: v for k, v in x.json().items() if k != "request_id"}  # noqa: E731
    check(f"non-member {label} == missing project (404, same body)", a.status_code == b.status_code == 404 and strip(a) == strip(b), f"{a.status_code}/{b.status_code}")
check("stranger user gets 404", stranger.get(f"{BASE}/dmrv/projects/{pid}").status_code == 404)
check("owner-b list excludes owner-a project", all(p["id"] != pid for p in owner_b.get(BASE + "/dmrv/projects").json()))

# --- membership
r = owner_a.post(f"{BASE}/dmrv/projects/{pid}/members", json={"principal_type": "user", "username": "field-user", "role": "field_operator"})
check("owner adds field operator (201)", r.status_code == 201, r.text[:160])
check("field operator now sees the project (200)", field_user.get(f"{BASE}/dmrv/projects/{pid}").status_code == 200)
check("field operator cannot add boundary (403)", field_user.post(f"{BASE}/dmrv/projects/{pid}/boundaries", json={"geojson": SQUARE, "reason": "x"}).status_code == 403)

# --- boundary + overlap (no id leak)
r = owner_a.post(f"{BASE}/dmrv/projects/{pid}/boundaries", json={"geojson": SQUARE, "reason": "initial"})
check("boundary v1 saved", r.status_code == 201 and r.json()["version"] == 1 and 120 < r.json()["area_ha"] < 125, r.text[:160])
bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
r = owner_a.post(f"{BASE}/dmrv/projects/{pid}/boundaries", json={"geojson": bowtie, "reason": "bad"})
check("bowtie rejected 422 invalid_geometry", r.status_code == 422 and r.json()["code"] == "invalid_geometry")
other = owner_b.post(BASE + "/dmrv/projects", json={"name": "Other " + RUN}, headers={"Idempotency-Key": "b-1-" + RUN}).json()["id"]
r = owner_b.post(f"{BASE}/dmrv/projects/{other}/boundaries", json={"geojson": SHIFTED, "reason": "b"})
body = r.json()
check("overlap reported to other owner as count+area only", r.status_code == 201 and body["overlap"]["other_projects"] == 1 and pid not in r.text and ("E2E Pilot " + RUN) not in r.text, r.text[:200])

# --- field data with the real Bu Dessy sample
sample = FIX / "bu_dessy_sample.csv"
form = {"height_unit": "cm", "plot_area_m2": "400", "area_basis": "assumed", "mode": "atomic"}
files = lambda p: {"file": (p.name, p.read_bytes(), "text/csv")}  # noqa: E731
r = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports/preview", files=files(sample), data=form)
check("operator preview ok: 21 rows, 0 errors", r.status_code == 200 and r.json()["report"]["rows_total"] == 21 and r.json()["report"]["errors"] == 0, r.text[:200])
r = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports", files=files(sample), data=form, headers={"Idempotency-Key": "imp-2024-" + RUN})
check("operator imports 21 trees (201 committed)", r.status_code == 201 and r.json()["status"] == "committed" and r.json()["rows_imported"] == 21, r.text[:200])
r2 = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports", files=files(sample), data=form, headers={"Idempotency-Key": "imp-2024-" + RUN})
check("import replay is 200 with same batch", r2.status_code == 200 and r2.json()["batch_id"] == r.json()["batch_id"])
r3 = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports", files=files(sample), data=form, headers={"Idempotency-Key": "imp-again-" + RUN})
check("re-import of same visit is rejected (tree_exists)", r3.json()["status"] == "rejected" and {i["code"] for i in r3.json()["issues"] if i["level"] == "error"} == {"tree_exists"})
syn = FIX / "bu_dessy_sample_synthetic_2022.csv"
r = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports", files=files(syn), data=form, headers={"Idempotency-Key": "imp-2022-" + RUN})
check("synthetic 2022 revisit adds a visit", r.status_code == 201 and r.json()["visits_created"] == 1)
visits = owner_a.get(f"{BASE}/dmrv/projects/{pid}/field-data/plots/1/visits").json()
check("two visits, year precision, assumed basis", [v["visit_date"] for v in visits] == ["2022-01-01", "2024-01-01"] and visits[1]["density"]["area_basis"] == "assumed")
carbon = visits[1]["density"]["carbon_mg_c_per_ha"]
check("2024 carbon density plausible (Mg C/ha)", carbon and 100 < carbon < 400, str(carbon))
check("analyst-less owner reads accounting? n/a; stranger cannot list plots", stranger.get(f"{BASE}/dmrv/projects/{pid}/field-data/plots").status_code == 404)

# --- limits
big = b"a,b\n" + b"1,2\n" * (3 * 1024 * 1024)
r = field_user.post(f"{BASE}/dmrv/projects/{pid}/field-data/imports/preview", files={"file": ("big.csv", big, "text/csv")}, data=form)
check("oversize upload is 413 file_too_large", r.status_code == 413 and r.json()["code"] == "file_too_large", f"{r.status_code} {r.text[:100]}")

# --- optimistic concurrency
r = owner_a.patch(f"{BASE}/dmrv/projects/{pid}", json={"name": "E2E Pilot v2 " + RUN}, headers={"If-Match": '"1"'})
check("PATCH with current revision ok", r.status_code == 200 and r.json()["revision"] == 2)
r = owner_a.patch(f"{BASE}/dmrv/projects/{pid}", json={"name": "lost"}, headers={"If-Match": '"1"'})
check("PATCH with stale revision is 412", r.status_code == 412 and r.json()["code"] == "revision_mismatch")

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
