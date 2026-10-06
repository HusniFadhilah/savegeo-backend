"""Seed configurable Viewer application feature permissions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_viewer_features"
down_revision: str | None = "0026_disaster_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FEATURES = {
    "app.feature.carbon_stock": "Viewer: estimasi stok karbon",
    "app.feature.land_cover": "Viewer: Land Cover / LULC",
    "app.feature.vegetation_indices": "Viewer: indeks vegetasi",
    "app.feature.land_cover_change": "Viewer: perubahan tutupan lahan",
    "app.feature.satellite_exploration": "Viewer: eksplorasi scene satelit",
    "app.feature.disaster_mapping": "Viewer: pemetaan bencana yang dipublikasikan",
    "app.feature.crop_monitoring": "Viewer: Crop Monitoring",
}
VIEWER_FEATURES_MANAGE_PERMISSION = "viewer_features.manage"
PERMISSION_OWNERSHIP_TABLE = "_viewer_feature_permission_ownership"
ROLE_PERMISSION_OWNERSHIP_TABLE = "_viewer_feature_role_permission_ownership"


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("feature_permissions_configured", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        PERMISSION_OWNERSHIP_TABLE,
        sa.Column("code", sa.String(length=128), primary_key=True),
    )
    op.create_table(
        ROLE_PERMISSION_OWNERSHIP_TABLE,
        sa.Column("role_id", sa.Integer(), primary_key=True),
        sa.Column("permission_id", sa.Integer(), primary_key=True),
    )
    bind = op.get_bind()
    permissions = {**FEATURES, VIEWER_FEATURES_MANAGE_PERMISSION: "Manage Viewer feature access"}
    for code, description in permissions.items():
        inserted = bind.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )
        if inserted.rowcount == 1:
            bind.execute(
                sa.text(f"INSERT INTO {PERMISSION_OWNERSHIP_TABLE} (code) VALUES (:code)"),
                {"code": code},
            )
    bind.execute(
        sa.text(
            "INSERT INTO roles (name, description, is_default, feature_permissions_configured) "
            "VALUES ('viewer', 'Role dengan akses fitur aplikasi yang ditentukan administrator', false, false) "
            "ON CONFLICT (name) DO NOTHING"
        )
    )
    viewers = bind.execute(sa.text("SELECT id FROM roles WHERE lower(name) = 'viewer' AND feature_permissions_configured = false")).fetchall()
    for (role_id,) in viewers:
        for code in FEATURES:
            permission_id = bind.execute(
                sa.text("SELECT id FROM permissions WHERE code = :code"), {"code": code}
            ).scalar_one()
            inserted = bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id) ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"role_id": role_id, "permission_id": permission_id},
            )
            if inserted.rowcount == 1:
                bind.execute(
                    sa.text(
                        f"INSERT INTO {ROLE_PERMISSION_OWNERSHIP_TABLE} (role_id, permission_id) "
                        "VALUES (:role_id, :permission_id)"
                    ),
                    {"role_id": role_id, "permission_id": permission_id},
                )
    bind.execute(
        sa.text("UPDATE roles SET feature_permissions_configured = true WHERE lower(name) = 'viewer'")
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE (role_id, permission_id) IN "
            f"(SELECT role_id, permission_id FROM {ROLE_PERMISSION_OWNERSHIP_TABLE})"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            f"(SELECT p.id FROM permissions p JOIN {PERMISSION_OWNERSHIP_TABLE} o ON o.code = p.code)"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM permissions WHERE code IN "
            f"(SELECT code FROM {PERMISSION_OWNERSHIP_TABLE})"
        )
    )
    op.drop_table(ROLE_PERMISSION_OWNERSHIP_TABLE)
    op.drop_table(PERMISSION_OWNERSHIP_TABLE)
    op.drop_column("roles", "feature_permissions_configured")
