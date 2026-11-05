import json
import uuid

from app.db.models.dmrv_field import ImportBatch, SurveyPlot
from tests.test_dmrv_projects import call, count, create, env  # noqa: F401  (env is a fixture)

HEADER = "plot_code,survey_year,tree_code,species,circumference_cm,dbh_cm,height,biomass_kg,latitude,longitude\n"
GOOD = ("1,2024,1,Asem,70,22.28,892,205.4,-7.0527,110.4396\n"
        "1,2024,2,Ketapang,59.3,18.88,825,137.8,-7.0527,110.4396\n")
BAD = "1,2024,3,Johar,abc,28.33,1248,382.8,-7.0527,110.4396\n"
PARAMS = {"height_unit": "cm", "plot_area_m2": "400", "area_basis": "assumed"}
MEMBER = {"principal_type": "user", "username": "field-user", "role": "field_operator"}


def upload(client, project_id, suffix="imports", *, body=GOOD, viewer="admin:1", key="f-1", name="trees.csv",
           raw=None, **form):
    content = raw if raw is not None else (HEADER + body).encode()
    return call(
        client, "POST", f"/dmrv/projects/{project_id}/field-data/{suffix}", viewer=viewer, key=key,
        files={"file": (name, content, "text/csv")}, data={**PARAMS, **form},
    )


def project(client):
    return create(client).json()["id"]


def test_owner_imports_csv_and_plots_are_listed(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    response = upload(client, pid, mode="atomic")
    assert response.status_code == 201
    body = response.json()
    assert (body["status"], body["rows_imported"], body["plots_created"]) == ("committed", 2, 1)
    plots = call(client, "GET", f"/dmrv/projects/{pid}/field-data/plots").json()
    assert [(p["plot_code"], p["visit_count"], p["area_basis"]) for p in plots] == [("1", 1, "assumed")]
    visits = call(client, "GET", f"/dmrv/projects/{pid}/field-data/plots/1/visits").json()
    assert visits[0]["density"]["carbon_mg_c_per_ha"] is not None and visits[0]["density"]["area_basis"] == "assumed"


def test_preview_does_not_write(env):  # noqa: F811
    client, factory = env
    pid = project(client)
    response = upload(client, pid, "imports/preview", body=GOOD + BAD, key=None)
    assert response.status_code == 200
    assert response.json()["report"]["errors"] == 1 and response.json()["suggested_mapping"]["plot_code"] == "plot_code"
    assert count(factory, ImportBatch) == 0 and count(factory, SurveyPlot) == 0


def test_atomic_is_the_default_and_rejects_on_any_error(env):  # noqa: F811
    client, factory = env
    pid = project(client)
    body = upload(client, pid, body=GOOD + BAD).json()
    assert body["mode"] == "atomic" and body["status"] == "rejected" and body["rows_imported"] == 0
    assert count(factory, SurveyPlot) == 0
    partial = upload(client, pid, body=GOOD + BAD, mode="partial", key="f-2").json()
    assert partial["status"] == "partial" and partial["rows_imported"] == 2


def test_field_operator_may_import_analyst_may_not(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    assert call(client, "POST", f"/dmrv/projects/{pid}/members", json=MEMBER).status_code == 201
    assert upload(client, pid, viewer="user:1", key="u-1").status_code == 201
    analyst = {"principal_type": "user", "username": "field-user", "role": "analyst"}
    # field-user is already a member; use a second project to check the analyst role.
    other = create(client, key="k-2", payload={"name": "Other"}).json()["id"]
    assert call(client, "POST", f"/dmrv/projects/{other}/members", json=analyst).status_code == 201
    forbidden = upload(client, other, viewer="user:1", key="u-2")
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden"


def test_non_member_import_is_identical_to_missing_project(env):  # noqa: F811
    client, factory = env
    pid = project(client)
    foreign = upload(client, pid, viewer="admin:2")
    missing = upload(client, str(uuid.uuid4()), viewer="admin:2")
    assert foreign.status_code == missing.status_code == 404
    strip = lambda r: {k: v for k, v in r.json().items() if k != "request_id"}  # noqa: E731
    assert strip(foreign) == strip(missing)
    assert count(factory, ImportBatch) == 0
    for suffix in ("plots", "plots/1/visits"):
        a = call(client, "GET", f"/dmrv/projects/{pid}/field-data/{suffix}", viewer="admin:2")
        b = call(client, "GET", f"/dmrv/projects/{uuid.uuid4()}/field-data/{suffix}", viewer="admin:2")
        assert a.status_code == b.status_code == 404 and strip(a) == strip(b)


def test_same_idempotency_key_replays_one_batch(env):  # noqa: F811
    client, factory = env
    pid = project(client)
    first = upload(client, pid, mode="atomic")
    second = upload(client, pid, mode="atomic")
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert count(factory, ImportBatch) == 1
    different = upload(client, pid, body=GOOD + "1,2024,9,X,50,15.9,500,50,-7.0527,110.4396\n", mode="atomic")
    assert different.status_code == 422 and different.json()["code"] == "idempotency_key_reused"


def test_commit_requires_idempotency_key(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    response = upload(client, pid, key=None)
    assert response.status_code == 400 and response.json()["code"] == "idempotency_key_required"


def test_upload_limits_and_types(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    too_big = upload(client, pid, raw=b"a,b\n" + b"1,2\n" * (3 * 1024 * 1024), key="big")
    assert too_big.status_code == 413 and too_big.json()["code"] == "file_too_large"
    wrong_type = upload(client, pid, name="trees.txt", key="txt")
    assert wrong_type.status_code == 422 and wrong_type.json()["code"] == "unsupported_file"


def test_bad_mapping_json_is_422(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    broken = upload(client, pid, mapping="{not json", key="m1")
    unknown = upload(client, pid, mapping=json.dumps({"colour": "plot_code"}), key="m2")
    assert broken.status_code == 422 and broken.json()["code"] == "bad_mapping"
    assert unknown.status_code == 422 and unknown.json()["code"] == "bad_mapping"


def test_density_is_blocked_without_area_basis(env):  # noqa: F811
    client, _ = env
    pid = project(client)
    response = upload(client, pid, plot_area_m2="", area_basis="", mode="atomic")
    assert response.status_code == 201
    density = call(client, "GET", f"/dmrv/projects/{pid}/field-data/plots/1/visits").json()[0]["density"]
    assert density["carbon_mg_c_per_ha"] is None and density["blockers"] == ["plot_area_unknown"]
