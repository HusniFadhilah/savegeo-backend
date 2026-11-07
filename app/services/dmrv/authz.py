"""Project-level authorization for dMRV.

Access is granted only by an explicit ProjectMember row. A missing project and
a project the principal is not a member of are indistinguishable
(``ProjectNotFound``), so resource existence is never leaked. Full-access admin
accounts (``role_id IS NULL``) get no special standing here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.admin_user import AdminUser
from app.db.models.dmrv_project import ProjectMember
from app.db.models.user import User
from app.services.dmrv.errors import DmrvError, project_not_found

PrincipalType = Literal["user", "admin"]

PROJECT_ROLES: frozenset[str] = frozenset(
    {"project_owner", "analyst", "field_operator", "reviewer", "verifier"}
)

_READ = frozenset({"project.read", "boundary.read", "field.read", "accounting.read", "monitoring.read", "evidence.read"})

# Separation of duties: no role may both create a submission and decide a review.
ROLE_ACTIONS: dict[str, frozenset[str]] = {
    "project_owner": _READ | {"project.write", "members.manage", "boundary.write", "field.write", "field.summary.run", "monitoring.write", "evidence.write", "submission.create"},
    "analyst": _READ | {"boundary.write", "field.summary.run", "monitoring.write", "evidence.write", "submission.create"},
    "field_operator": frozenset({"project.read", "boundary.read", "field.read", "field.write", "monitoring.read"}),
    "reviewer": _READ | {"review.decide"},
    "verifier": _READ | {"review.decide"},
}


class ProjectNotFound(Exception):
    """No such project, or the principal is not a member of it."""


class ProjectForbidden(Exception):
    """The principal is a member, but their role does not allow the action."""


@dataclass(frozen=True)
class Principal:
    type: PrincipalType
    id: int

    @property
    def key(self) -> str:
        return f"{self.type}:{self.id}"

    @classmethod
    def from_account(cls, account: User | AdminUser) -> Principal:
        return cls("user" if isinstance(account, User) else "admin", int(account.id))


def get_member_role(db: Session, project_id: str, principal: Principal) -> str | None:
    return db.execute(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_type == principal.type,
            ProjectMember.principal_id == principal.id,
        )
    ).scalar_one_or_none()


def require_project_action(db: Session, project_id: str, principal: Principal, action: str) -> str:
    role = get_member_role(db, project_id, principal)
    if role is None:
        raise ProjectNotFound(project_id)
    if action not in ROLE_ACTIONS.get(role, frozenset()):
        raise ProjectForbidden(action)
    return role


def authorize(db: Session, project_id: str, principal: Principal, action: str) -> str:
    """API-facing wrapper: uniform 404 for non-members, 403 for a wrong role."""
    try:
        return require_project_action(db, project_id, principal, action)
    except ProjectNotFound:
        raise project_not_found() from None
    except ProjectForbidden:
        raise DmrvError(403, "forbidden", "Peran Anda tidak memiliki izin untuk aksi ini.") from None
