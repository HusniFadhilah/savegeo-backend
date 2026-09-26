"""Create or update the four documented SaveGeo test accounts.

Passwords are intentionally read only from the process environment.  They are
never stored in this repository, printed, or written to deployment logs.

Usage (on the application server)::

    export SAVEGEO_PASSWORD_SAVEGEOGEOSPATIAL='...'
    export SAVEGEO_PASSWORD_EXAMPLE_USER='...'
    export SAVEGEO_PASSWORD_DEMO_ADMIN='...'
    export SAVEGEO_PASSWORD_SUPER_ADMIN='...'
    python -m scripts.seed_rbac
    python -m scripts.ensure_test_accounts

The script is idempotent: an existing account is updated with the supplied
password, active state, and role.  ``super_admin`` uses ``role_id = NULL``,
which is the application's explicit legacy full-access state.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from app.core.security import hash_password
from app.db.models.admin_user import AdminUser
from app.db.models.role import Role
from app.db.session import SessionLocal
from scripts.seed_rbac import seed_rbac


@dataclass(frozen=True)
class TestAccount:
    username: str
    password_env: str
    role_name: str | None


TEST_ACCOUNTS = (
    TestAccount("SavegeoGeospatial", "SAVEGEO_PASSWORD_SAVEGEOGEOSPATIAL", "geospatial_expert"),
    TestAccount("example_user", "SAVEGEO_PASSWORD_EXAMPLE_USER", "viewer"),
    TestAccount("demo_admin", "SAVEGEO_PASSWORD_DEMO_ADMIN", "admin"),
    TestAccount("super_admin", "SAVEGEO_PASSWORD_SUPER_ADMIN", None),
)


def _passwords_from_environment() -> dict[str, str]:
    missing = [account.password_env for account in TEST_ACCOUNTS if not os.getenv(account.password_env)]
    if missing:
        print(
            "Missing required password environment variables: " + ", ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(1)

    passwords = {account.username: os.environ[account.password_env] for account in TEST_ACCOUNTS}
    too_short = [username for username, password in passwords.items() if len(password) < 8]
    if too_short:
        print("Each test account password must be at least 8 characters.", file=sys.stderr)
        raise SystemExit(1)
    return passwords


def ensure_test_accounts() -> None:
    passwords = _passwords_from_environment()

    # Ensure role rows and their permission links exist before assigning them.
    seed_rbac()

    db = SessionLocal()
    try:
        roles = {role.name: role for role in db.query(Role).all()}
        for account in TEST_ACCOUNTS:
            if account.role_name is not None and account.role_name not in roles:
                raise RuntimeError(f"RBAC role {account.role_name!r} was not seeded")

            admin = db.query(AdminUser).filter_by(username=account.username).first()
            action = "updated"
            if admin is None:
                admin = AdminUser(username=account.username)
                db.add(admin)
                action = "created"

            admin.password_hash = hash_password(passwords[account.username])
            admin.is_active = True
            admin.role_id = roles[account.role_name].id if account.role_name else None
            print(f"{action} {account.username} ({account.role_name or 'full-access'})")

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    ensure_test_accounts()
