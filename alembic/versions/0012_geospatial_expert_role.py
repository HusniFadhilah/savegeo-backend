"""Add granular admin permissions and the geospatial expert role."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_geospatial_expert_role"
down_revision: str | None = "0011_analysis_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_PERMISSIONS = {
    "config.read": "View system configuration",
    "config.write": "Update system configuration",
    "model.read": "View uploaded models",
    "model.write": "Upload/update/delete models",
    "credential.read": "View GEE credential metadata",
    "credential.write": "Upload/activate/delete GEE credentials",
    "dataset.read": "View dataset catalog overrides",
    "dataset.write": "Toggle/edit dataset catalog overrides",
    "audit.read": "View audit log",
    "disaster.create": "Create disaster events",
    "disaster.update": "Edit disaster event metadata",
    "disaster.delete": "Delete disaster events",
    "disaster.aoi.write": "Create/update disaster event AOI",
    "disaster.imagery.write": "Configure pre/post satellite imagery",
    "disaster.analysis.configure": "Attach analysis models to a disaster event",
    "disaster.analysis.run": "Run/re-run an analysis",
    "disaster.analysis.publish": "Publish an analysis result",
    "disaster.analysis.unpublish": "Unpublish an analysis result",
    "disaster.result.write": "Edit analysis result metadata",
    "disaster.result.delete": "Delete an analysis result",
}

NEW_PERMISSIONS = {
    "users.read": "View administrator accounts and roles",
    "users.write": "Create/update/delete administrator accounts",
    "company.read": "View company boundaries",
    "company.write": "Create/update/delete/import company boundaries",
    "geospatial.read": "View cloud geospatial datasets and jobs",
    "geospatial.write": "Register/delete/export/query geospatial datasets",
    "satellite.read": "View satellite provider configuration",
    "satellite.write": "Update satellite provider configuration",
    "disaster.read": "View disaster management records",
    "secret.read": "View secret and API-key metadata",
    "secret.write": "Update secret and API-key configuration",
}

PERMISSIONS = {**LEGACY_PERMISSIONS, **NEW_PERMISSIONS}
SENSITIVE_PERMISSIONS = {"credential.read", "credential.write", "secret.read", "secret.write"}
ADMIN_PERMISSIONS = set(PERMISSIONS) - SENSITIVE_PERMISSIONS

GEOSPATIAL_EXPERT_PERMISSIONS = {
    "company.read",
    "company.write",
    "geospatial.read",
    "geospatial.write",
    "model.read",
    "model.write",
    "disaster.read",
    "disaster.create",
    "disaster.update",
    "disaster.delete",
    "disaster.aoi.write",
    "disaster.imagery.write",
    "disaster.analysis.configure",
    "disaster.analysis.run",
    "disaster.analysis.publish",
    "disaster.analysis.unpublish",
    "disaster.result.write",
    "disaster.result.delete",
    "audit.read",
}


def upgrade() -> None:
    bind = op.get_bind()
    for code, description in PERMISSIONS.items():
        bind.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description"
            ),
            {"code": code, "description": description},
        )

    bind.execute(
        sa.text(
            "INSERT INTO roles (name, description, is_default) VALUES "
            "(:name, :description, false) ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description"
        ),
        {
            "name": "geospatial_expert",
            "description": "Ahli geospasial: batas perusahaan, pemetaan bencana, model ML, dan data geospasial",
        },
    )

    bind.execute(
        sa.text(
            "INSERT INTO roles (name, description, is_default) VALUES "
            "(:name, :description, true) ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description"
        ),
        {"name": "admin", "description": "All non-secret permissions"},
    )

    # Explicit Admin is broad but cannot access credentials, API keys, secrets,
    # or key-pool entries. Legacy NULL-role admins are Full Access and bypass
    # permission checks by design.
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id = "
            "(SELECT id FROM roles WHERE name = 'admin')"
        )
    )
    for code in ADMIN_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
                "WHERE r.name = :role_name AND p.code = :code ON CONFLICT DO NOTHING"
            ),
            {"role_name": "admin", "code": code},
        )

    # Replace the assignment for this built-in role so a re-run cannot leave
    # an accidental sensitive permission attached to it.
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id = "
            "(SELECT id FROM roles WHERE name = 'geospatial_expert')"
        )
    )
    for code in GEOSPATIAL_EXPERT_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (role_id, permission_id) "
                "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
                "WHERE r.name = :role_name AND p.code = :code ON CONFLICT DO NOTHING"
            ),
            {"role_name": "geospatial_expert", "code": code},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE role_id = "
            "(SELECT id FROM roles WHERE name = 'geospatial_expert')"
        )
    )
    bind.execute(sa.text("DELETE FROM roles WHERE name = 'geospatial_expert'"))
    for code in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id = "
                "(SELECT id FROM permissions WHERE code = :code)"
            ),
            {"code": code},
        )
        bind.execute(sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code})
