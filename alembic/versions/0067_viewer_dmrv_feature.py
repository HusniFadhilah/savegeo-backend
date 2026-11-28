"""Add Viewer access for dMRV workspaces and organizations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_viewer_dmrv_feature"
down_revision: str | None = "0066_merge_green_carbon"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_CODE = "app.feature.dmrv_projects"
LEGACY_FEATURE_CODES = (
    "app.feature.carbon_stock",
    "app.feature.land_cover",
    "app.feature.vegetation_indices",
    "app.feature.land_cover_change",
    "app.feature.satellite_exploration",
    "app.feature.disaster_mapping",
    "app.feature.crop_monitoring",
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (code, description) VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {
            "code": FEATURE_CODE,
            "description": "Viewer: ruang kerja dMRV dan organisasi",
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r CROSS JOIN permissions p "
            "WHERE r.is_viewer IS TRUE AND p.code = :feature_code "
            "AND (SELECT count(DISTINCT old_permission.code) "
            "FROM role_permissions old_link "
            "JOIN permissions old_permission ON old_permission.id = old_link.permission_id "
            "WHERE old_link.role_id = r.id AND old_permission.code = ANY(:legacy_codes)) = :legacy_count "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        ),
        {
            "feature_code": FEATURE_CODE,
            "legacy_codes": list(LEGACY_FEATURE_CODES),
            "legacy_count": len(LEGACY_FEATURE_CODES),
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = :code)"
        ),
        {"code": FEATURE_CODE},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE code = :code"),
        {"code": FEATURE_CODE},
    )
