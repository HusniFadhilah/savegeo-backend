"""Atomic dMRV audit recorder.

Unlike ``app.services.audit_service.log_audit`` (best-effort, own commit), this
only adds and flushes a row in the caller's session. The caller commits the
state change and the audit event together; any failure here propagates so the
whole transition rolls back.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.dmrv_audit import DmrvAuditEvent
from app.services.dmrv.authz import Principal


def record_audit(
    db: Session,
    *,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: str | None,
    project_id: str | None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    snapshot_hash: str | None = None,
) -> DmrvAuditEvent:
    event = DmrvAuditEvent(
        project_id=project_id,
        actor_type=principal.type,
        actor_id=principal.id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
        reason=reason,
        request_id=request_id,
        snapshot_hash=snapshot_hash,
    )
    db.add(event)
    db.flush()
    return event
