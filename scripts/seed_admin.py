"""Create the first admin user, if none exists yet.

Unlike the legacy backend (which hardcoded `admin`/`admin123` on first boot — a real
security smell), this script requires an explicit username/password so no default
credential ever ships. Run interactively or pass env vars:

    SEED_ADMIN_USERNAME=husni SEED_ADMIN_PASSWORD='...' python -m scripts.seed_admin
"""
from __future__ import annotations

import getpass
import os
import sys

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.db.session import SessionLocal


def seed_default_admin() -> None:
    db = SessionLocal()
    try:
        if db.query(AdminUser).count() > 0:
            print("An admin user already exists — nothing to do.")
            return

        username = os.getenv("SEED_ADMIN_USERNAME") or input("Admin username: ").strip()
        password = os.getenv("SEED_ADMIN_PASSWORD") or getpass.getpass("Admin password (min 8 chars): ")
        if not username or not password or len(password) < 8:
            print("Username required and password must be at least 8 characters.", file=sys.stderr)
            sys.exit(1)

        admin = AdminUser(username=username, password_hash=hash_password(password), is_active=True)
        db.add(admin)
        db.commit()
        print(f"Created admin user '{username}'.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_default_admin()
