"""Cross-account / cross-project isolation for the dMRV API."""
import json
import uuid

from sqlalchemy import select

from app.db.models.dmrv_project import ProjectMember
from tests.test_dmrv_projects import PROJECT, SQUARE, call, create, env  # noqa: F401  (env is a fixture)

OVERLAPPING = {
    "type": "Polygon",
    "coordinates": [[[110.005, -7.0], [110.015, -7.0], [110.015, -6.99], [110.005, -6.99], [110.005, -7.0]]],
}
MEMBER = {"principal_type": "user", "username": "field-user", "role": "field_operator"}


def _normalised(response) -> tuple[int, dict]:
    body = response.json()
    body.pop("request_id", None)
    return response.status_code, body


def test_non_member_and_nonexistent_project_responses_are_identical(env):  # noqa: F811
    client, _ = env
    real_id = create(client, viewer="admin:1").json()["id"]
    fake_id = str(uuid.uuid4())
    attempts = [
        ("GET", "", None),
        ("PATCH", "", {"name": "x"}),
        ("POST", "/members", MEMBER),
        ("POST", "/boundaries", {"geojson": SQUARE, "reason": "x"}),
        ("GET", "/boundaries", None),
    ]
    for method, suffix, body in attempts:
        kwargs = {"json": body} if body is not None else {}
        foreign = call(client, method, f"/dmrv/projects/{real_id}{suffix}", viewer="admin:2", if_match='"1"', **kwargs)
        missing = call(client, method, f"/dmrv/projects/{fake_id}{suffix}", viewer="admin:2", if_match='"1"', **kwargs)
        assert foreign.status_code == 404, (method, suffix)
        assert _normalised(foreign) == _normalised(missing), (method, suffix)
        assert real_id not in foreign.text


def test_list_projects_never_includes_foreign_projects(env):  # noqa: F811
    client, _ = env
    first = create(client, viewer="admin:1", key="a", payload={**PROJECT, "name": "A"}).json()["id"]
    second = create(client, viewer="admin:2", key="b", payload={**PROJECT, "name": "B"}).json()["id"]
    ids_for_1 = [p["id"] for p in call(client, "GET", "/dmrv/projects", viewer="admin:1").json()]
    ids_for_2 = [p["id"] for p in call(client, "GET", "/dmrv/projects", viewer="admin:2").json()]
    assert ids_for_1 == [first] and ids_for_2 == [second]
    assert call(client, "GET", "/dmrv/projects", viewer="user:1").json() == []


def test_user_1_and_admin_1_do_not_share_membership(env):  # noqa: F811
    client, factory = env
    project_id = create(client, viewer="admin:1").json()["id"]
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 404
    with factory() as db:
        members = db.execute(select(ProjectMember.principal_type, ProjectMember.principal_id)).all()
    assert members == [("admin", 1)]


def test_overlap_warning_reveals_only_count_and_area(env):  # noqa: F811
    client, _ = env
    owner_a = create(client, viewer="admin:1", key="a", payload={**PROJECT, "name": "Rahasia Proyek A"}).json()["id"]
    assert call(client, "POST", f"/dmrv/projects/{owner_a}/boundaries", viewer="admin:1",
                json={"geojson": SQUARE, "reason": "A"}).status_code == 201
    owner_b = create(client, viewer="admin:2", key="b", payload={**PROJECT, "name": "B"}).json()["id"]
    response = call(client, "POST", f"/dmrv/projects/{owner_b}/boundaries", viewer="admin:2",
                    json={"geojson": OVERLAPPING, "reason": "B"})
    assert response.status_code == 201
    overlap = response.json()["overlap"]
    assert overlap["other_projects"] == 1 and overlap["overlap_ha"] > 0
    assert set(overlap) == {"other_projects", "overlap_ha"}
    assert owner_a not in response.text and "Rahasia Proyek A" not in response.text
    assert owner_a not in json.dumps(call(client, "GET", f"/dmrv/projects/{owner_b}/boundaries", viewer="admin:2").json())


def test_field_operator_member_is_forbidden_not_hidden(env):  # noqa: F811
    client, _ = env
    project_id = create(client, viewer="admin:1").json()["id"]
    assert call(client, "POST", f"/dmrv/projects/{project_id}/members", viewer="admin:1", json=MEMBER).status_code == 201
    assert call(client, "GET", f"/dmrv/projects/{project_id}", viewer="user:1").status_code == 200
    forbidden = [
        call(client, "POST", f"/dmrv/projects/{project_id}/members", viewer="user:1",
             json={"principal_type": "admin", "username": "owner-b", "role": "analyst"}),
        call(client, "POST", f"/dmrv/projects/{project_id}/boundaries", viewer="user:1",
             json={"geojson": SQUARE, "reason": "x"}),
        call(client, "PATCH", f"/dmrv/projects/{project_id}", viewer="user:1", if_match='"1"', json={"name": "x"}),
    ]
    assert [r.status_code for r in forbidden] == [403, 403, 403]
    assert all(r.json()["code"] == "forbidden" for r in forbidden)
