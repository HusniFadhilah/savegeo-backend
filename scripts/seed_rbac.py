"""Seed default roles + permissions (idempotent). Existing admins with
``role_id IS NULL`` remain Full Access; explicitly assigned built-in roles are
reconciled to their declared permission sets.

Usage: python -m scripts.seed_rbac
"""
from __future__ import annotations

from app.db.models.role import Permission, Role
from app.db.session import SessionLocal

PERMISSIONS = [
    ("viewer_features.manage", "Manage Viewer feature access"),
    ("app.feature.carbon_stock", "Viewer: estimasi stok karbon"),
    ("app.feature.land_cover", "Viewer: Land Cover / LULC"),
    ("app.feature.vegetation_indices", "Viewer: indeks vegetasi"),
    ("app.feature.land_cover_change", "Viewer: perubahan tutupan lahan"),
    ("app.feature.satellite_exploration", "Viewer: eksplorasi scene satelit"),
    ("app.feature.disaster_mapping", "Viewer: pemetaan bencana yang dipublikasikan"),
    ("app.feature.crop_monitoring", "Viewer: Crop Monitoring"),
    ("config.read", "View system configuration"),
    ("config.write", "Update system configuration"),
    ("model.read", "View uploaded models"),
    ("model.write", "Upload/update/delete models"),
    ("calibration.read", "View carbon calibration datasets and runs"),
    ("calibration.write", "Create/update/run carbon calibration workflows"),
    ("credential.read", "View GEE credential metadata"),
    ("credential.write", "Upload/activate/delete GEE credentials"),
    ("secret.read", "View secret and API-key metadata"),
    ("secret.write", "Update secret and API-key configuration"),
    ("dataset.read", "View dataset catalog overrides"),
    ("dataset.write", "Toggle/edit dataset catalog overrides"),
    ("audit.read", "View audit log"),
    ("users.read", "View administrator accounts and roles"),
    ("users.write", "Create/update/delete administrator accounts"),
    ("company.read", "View company boundaries"),
    ("company.write", "Create/update/delete/import company boundaries"),
    ("geospatial.read", "View cloud geospatial datasets and jobs"),
    ("geospatial.write", "Register/delete/export/query geospatial datasets"),
    ("satellite.read", "View satellite provider configuration"),
    ("satellite.write", "Update satellite provider configuration"),
    ("disaster.read", "View disaster management records"),
    ("disaster.create", "Create disaster events"),
    ("disaster.update", "Edit disaster event metadata"),
    ("disaster.delete", "Delete disaster events"),
    ("disaster.aoi.write", "Create/update disaster event AOI"),
    ("disaster.imagery.write", "Configure pre/post satellite imagery"),
    ("disaster.analysis.configure", "Attach analysis models to a disaster event"),
    ("disaster.analysis.run", "Run/re-run an analysis"),
    ("disaster.analysis.publish", "Publish an analysis result"),
    ("disaster.analysis.unpublish", "Unpublish an analysis result"),
    ("disaster.result.write", "Edit analysis result metadata"),
    ("disaster.result.delete", "Delete an analysis result"),
]

SENSITIVE_PERMISSIONS = {"credential.read", "credential.write", "secret.read", "secret.write"}
ADMIN_PERMISSIONS = [code for code, _ in PERMISSIONS if code not in SENSITIVE_PERMISSIONS]
VIEWER_PERMISSIONS = [
    code for code, _ in PERMISSIONS
    if code.endswith(".read") and code not in {"credential.read", "secret.read"}
]
VIEWER_FEATURE_PERMISSIONS = [
    "app.feature.carbon_stock",
    "app.feature.land_cover",
    "app.feature.vegetation_indices",
    "app.feature.land_cover_change",
    "app.feature.satellite_exploration",
    "app.feature.disaster_mapping",
    "app.feature.crop_monitoring",
]

ROLES = [
    ("admin", "All non-secret permissions", True, ADMIN_PERMISSIONS),
    ("viewer", "Akses fitur aplikasi yang ditentukan administrator", False, VIEWER_PERMISSIONS + VIEWER_FEATURE_PERMISSIONS),
    (
        "geospatial_expert",
        "Ahli geospasial: batas perusahaan, pemetaan bencana, model ML, dan data geospasial",
        False,
        [
            "company.read",
            "company.write",
            "geospatial.read",
            "geospatial.write",
            "model.read",
            "model.write",
            "satellite.read",
            "satellite.write",
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
        ],
    ),
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
            if name == "viewer":
                # Reconcile historic read-only grants without touching an
                # administrator's feature selection, including an intentionally
                # empty selection. New roles receive the seven safe defaults.
                assigned = [perm_by_code[c] for c in VIEWER_PERMISSIONS]
                if role.feature_permissions_configured:
                    configured_features = [p for p in role.permissions if p.code in VIEWER_FEATURE_PERMISSIONS]
                    role.permissions = assigned + configured_features
                else:
                    role.permissions = assigned + [perm_by_code[c] for c in VIEWER_FEATURE_PERMISSIONS]
                    role.feature_permissions_configured = True
            else:
                # Only the built-in Admin role receives this narrow Viewer
                # feature-management grant. Full Access remains role_id NULL.
                role.permissions = [perm_by_code[c] for c in perm_codes]

        db.commit()
        print(f"Seeded {len(PERMISSIONS)} permissions and {len(ROLES)} roles.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_rbac()
