"""Probe before activation, short DB locks, and compensating rollback on init failure."""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.audit_log import AuditLog
from app.db.models.gee_credential import GEECredential
from app.services import credential_probe, gee_service, storage_service


class CredentialActivationError(RuntimeError):
    def __init__(self, code: str, runtime_initialized: bool | None = None):
        super().__init__("Kredensial belum diaktifkan. Periksa hasil pemeriksaan koneksi.")
        self.code = code
        self.runtime_initialized = runtime_initialized


def check(db: Session, credential_id: int, actor_id: int) -> dict:
    row = db.get(GEECredential, credential_id)
    if row is None:
        raise CredentialActivationError("credential_missing")
    path = row.bucket_path
    db.rollback()  # no DB lock/transaction during storage/network IO
    try:
        result = credential_probe.probe(storage_service.download_credential(path))
    except Exception:  # noqa: BLE001 - secrets and provider bodies never enter metadata
        result = credential_probe.ProbeResult(False, "credential_unavailable", dt.datetime.now(dt.UTC).isoformat())
    row = db.get(GEECredential, credential_id)
    if row is None or row.bucket_path != path:
        raise CredentialActivationError("credential_changed")
    row.last_checked_at = dt.datetime.fromisoformat(result.checked_at)
    row.last_check_status, row.last_check_code = ("ok" if result.ok else "failed"), result.code
    db.add(AuditLog(admin_user_id=actor_id, action="credential.probe", resource_type="gee_credential",
                    resource_id=str(row.id), detail={"ok": result.ok, "code": result.code}))
    db.commit()
    return result.to_dict()


def activate(db: Session, credential_id: int, actor_id: int) -> dict:
    result = check(db, credential_id, actor_id)
    if not result["ok"]:
        raise CredentialActivationError(result["code"])
    rows = list(db.scalars(select(GEECredential).order_by(GEECredential.id).with_for_update()))
    candidate = next((row for row in rows if row.id == credential_id), None)
    if candidate is None:
        raise CredentialActivationError("credential_missing")
    previous = [row.id for row in rows if row.is_active]
    for row in rows:
        row.is_active = row.id == credential_id
    db.add(AuditLog(admin_user_id=actor_id, action="credential.activate", resource_type="gee_credential",
                    resource_id=str(credential_id), detail={"previous_ids": previous, "probe": result["code"]}))
    db.commit()
    if gee_service.initialize_ee(db):
        return {"gee_initialized": True, "probe": result}
    # Restore only if nobody has activated another credential in the meantime.
    rows = list(db.scalars(select(GEECredential).order_by(GEECredential.id).with_for_update()))
    if [row.id for row in rows if row.is_active] == [credential_id]:
        for row in rows:
            row.is_active = row.id in previous
        db.add(AuditLog(admin_user_id=actor_id, action="credential.activation.rollback", resource_type="gee_credential",
                        resource_id=str(credential_id), detail={"restored_ids": previous, "code": "runtime_initialization_failed"}))
        db.commit()
    else:
        db.rollback()
    restored = gee_service.initialize_ee(db)
    raise CredentialActivationError("runtime_initialization_failed", runtime_initialized=restored)
