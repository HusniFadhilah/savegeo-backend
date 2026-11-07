"""Seed the THROWAWAY e2e database only (refuses any other DATABASE_URL)."""
import os
import sys
from urllib.parse import urlsplit

url = os.environ.get("DATABASE_URL", "")
try:
    parsed_url = urlsplit(url)
    safe_target = (
        parsed_url.scheme in {"postgres", "postgresql", "postgresql+psycopg"}
        and parsed_url.hostname == "127.0.0.1"
        and parsed_url.path == "/e2e"
        and parsed_url.port in {55433, 55434}
    )
except ValueError:
    safe_target = False
if not safe_target:
    sys.exit(f"refusing to seed: DATABASE_URL is not the throwaway e2e database ({url!r})")

from app.core.security import hash_password  # noqa: E402
from app.db.models.admin_user import AdminUser  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from scripts.seed_rbac import seed_rbac as seed_rbac_main  # noqa: E402

PASSWORD = "E2e-Passw0rd!"
seed_rbac_main()
with SessionLocal() as db:
    for name in ("owner-a", "owner-b"):
        db.add(AdminUser(username=name, full_name=name.title(), email=f"{name}@e2e.test", password_hash=hash_password(PASSWORD), is_active=True))
    db.add(User(username="field-user", full_name="Field User", email="field@e2e.test", password_hash=hash_password(PASSWORD), is_active=True))
    db.add(User(username="stranger", full_name="Stranger", email="stranger@e2e.test", password_hash=hash_password(PASSWORD), is_active=True))
    db.commit()
print("seeded")
