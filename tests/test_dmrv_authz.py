import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.user import User
from app.services.dmrv.authz import (
    PROJECT_ROLES,
    ROLE_ACTIONS,
    Principal,
    ProjectForbidden,
    ProjectNotFound,
    get_member_role,
    require_project_action,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    DmrvProject.__table__.create(engine)
    ProjectMember.__table__.create(engine)
    with Session(engine) as session:
        session.add(DmrvProject(id="p-1", name="Pilot"))
        session.commit()
        yield session
    engine.dispose()


def _member(db, principal_type, principal_id, role, project_id="p-1"):
    db.add(
        ProjectMember(
            project_id=project_id, principal_type=principal_type, principal_id=principal_id,
            role=role, added_by="admin:99",
        )
    )
    db.commit()


def test_principal_key_and_from_account():
    assert Principal("user", 7).key == "user:7"
    assert Principal.from_account(User(id=3, username="u", email="u@x.test", password_hash="x")) == Principal("user", 3)
    assert Principal.from_account(AdminUser(id=5, username="a", email="a@x.test", password_hash="x")) == Principal("admin", 5)


def test_non_member_and_missing_project_both_raise_not_found(db):
    _member(db, "admin", 1, "project_owner")
    with pytest.raises(ProjectNotFound):
        require_project_action(db, "p-1", Principal("admin", 2), "project.read")
    with pytest.raises(ProjectNotFound):
        require_project_action(db, "does-not-exist", Principal("admin", 1), "project.read")


def test_user_and_admin_with_same_numeric_id_are_distinct(db):
    _member(db, "admin", 1, "project_owner")
    assert get_member_role(db, "p-1", Principal("admin", 1)) == "project_owner"
    assert get_member_role(db, "p-1", Principal("user", 1)) is None
    with pytest.raises(ProjectNotFound):
        require_project_action(db, "p-1", Principal("user", 1), "project.read")


def test_member_without_row_is_not_found_even_for_full_access_admin(db):
    # An admin account with role_id NULL has no special standing for project data.
    with pytest.raises(ProjectNotFound):
        require_project_action(db, "p-1", Principal("admin", 42), "members.manage")


def test_project_owner_and_analyst_can_run_field_summary_but_field_operator_cannot(db):
    _member(db, "admin", 1, "project_owner")
    _member(db, "admin", 2, "analyst")
    _member(db, "user", 4, "field_operator")
    assert require_project_action(db, "p-1", Principal("admin", 1), "field.summary.run") == "project_owner"
    assert require_project_action(db, "p-1", Principal("admin", 2), "field.summary.run") == "analyst"
    with pytest.raises(ProjectForbidden):
        require_project_action(db, "p-1", Principal("user", 4), "field.summary.run")


def test_monitoring_period_permissions_are_explicit(db):
    _member(db, "admin", 1, "project_owner")
    _member(db, "admin", 2, "analyst")
    _member(db, "user", 4, "field_operator")
    assert require_project_action(db, "p-1", Principal("admin", 1), "monitoring.write") == "project_owner"
    assert require_project_action(db, "p-1", Principal("admin", 2), "monitoring.write") == "analyst"
    assert require_project_action(db, "p-1", Principal("user", 4), "monitoring.read") == "field_operator"
    with pytest.raises(ProjectForbidden):
        require_project_action(db, "p-1", Principal("user", 4), "monitoring.write")


def test_field_operator_can_write_field_data_but_not_read_accounting(db):
    _member(db, "user", 4, "field_operator")
    principal = Principal("user", 4)
    assert require_project_action(db, "p-1", principal, "field.write") == "field_operator"
    with pytest.raises(ProjectForbidden):
        require_project_action(db, "p-1", principal, "accounting.read")


def test_no_role_can_both_create_submission_and_decide_review():
    for role, actions in ROLE_ACTIONS.items():
        assert not ({"submission.create", "review.decide"} <= actions), role


def test_role_matrix_covers_exactly_the_project_roles():
    assert set(ROLE_ACTIONS) == set(PROJECT_ROLES)
    assert "members.manage" in ROLE_ACTIONS["project_owner"]
    assert "members.manage" not in ROLE_ACTIONS["analyst"]
