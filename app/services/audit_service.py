"""Append-only audit logging for admin mutations. Best-effort: a logging
failure never blocks the actual admin action (the log write is wrapped so a
DB hiccup here cannot turn a successful config/model/credential change into a
500 for the admin).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


def log_audit(
    db: Session,
    admin_user_id: int | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    detail: dict | None = None,
    ip_address: str | None = None,
) -> None:
    try:
        db.add(AuditLog(
            admin_user_id=admin_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            detail=detail,
            ip_address=ip_address,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit log write failed (non-fatal): %s", exc)
        db.rollback()
