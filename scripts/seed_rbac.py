"""Seed default roles + permissions (idempotent). Does NOT touch existing
admin_users.role_id (stays NULL = legacy full-access), so running this is
a zero-risk additive step - no existing admin loses or gains access.

Usage: python -m scripts.seed_rbac
"""
from __future__ import annotations

from app.db.models.role import Permission, Role
from app.db.session import SessionLocal

PERMISSIONS = [
    ("config.read", "View system configuration"),
    ("config.write", "Update system configuration"),
    ("model.read", "View uploaded models"),
    ("model.write", "Upload/update/delete models"),
    ("credential.read", "View GEE credential metadata"),
    ("credential.write", "Upload/activate/delete GEE credentials"),
    ("dataset.read", "View dataset catalog overrides"),
    ("dataset.write", "Toggle/edit dataset catalog overrides"),
    ("audit.read", "View audit log"),
]

ROLES = [
    ("admin", "Full access (all permissions)", True, [code for code, _ in PERMISSIONS]),
    ("viewer", "Read-only access", False, [c for c, _ in PERMISSIONS if c.endswith(".read")]),
]


def seed_rbac() -> None:
    db = SessionLocal()
    try:
        perm_by_code = {}
        for code, description in PERMISSIONS:
            row = db.query(Permission).filter_by(code=code).first()
            if row is None:
                row = Permission(code=code, description=description)
                db.add(row)
                db.flush()
            perm_by_code[code] = row

        for name, description, is_default, perm_codes in ROLES:
            role = db.query(Role).filter_by(name=name).first()
            if role is None:
                role = Role(name=name, description=description, is_default=is_default)
                db.add(role)
                db.flush()
            role.permissions = [perm_by_code[c] for c in perm_codes]

        db.commit()
        print(f"Seeded {len(PERMISSIONS)} permissions and {len(ROLES)} roles.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_rbac()
