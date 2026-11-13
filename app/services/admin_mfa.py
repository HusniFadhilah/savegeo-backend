"""Persistent MFA challenges, replay prevention and single-use recovery codes.

Lock order is account -> factor -> challenge. Failed attempts commit their
counter before returning an error, so another API process cannot reset limits.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from typing import NoReturn
from urllib.parse import quote, urlencode

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import secret_encryption, security, totp
from app.db.models.admin_mfa import AdminMfa, MfaChallenge
from app.db.models.admin_user import AdminUser
from app.db.models.audit_log import AuditLog


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.UTC)


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _audit(db: Session, admin_id: int, action: str) -> None:
    db.add(AuditLog(admin_user_id=admin_id, action=action, resource_type="admin_mfa", resource_id=str(admin_id), detail={}))


def _locked(db: Session, admin_id: int) -> tuple[AdminUser, AdminMfa]:
    admin = db.scalar(select(AdminUser).where(AdminUser.id == admin_id).with_for_update())
    if admin is None or not admin.is_active:
        raise HTTPException(401, "Invalid or expired MFA challenge")
    factor = db.scalar(select(AdminMfa).where(AdminMfa.admin_id == admin_id).with_for_update())
    if factor is None:
        factor = AdminMfa(admin_id=admin_id, recovery_hashes=[], last_counter=-1, failures=0)
        db.add(factor)
        db.flush()
    return admin, factor


def _check_lock(factor: AdminMfa, now: dt.datetime) -> None:
    if factor.locked_until and _utc(factor.locked_until) > now:
        raise HTTPException(429, "Terlalu banyak percobaan MFA. Coba lagi setelah lima menit.")


def begin(db: Session, admin: AdminUser, now: dt.datetime | None = None) -> dict:
    moment = _now(now)
    admin, factor = _locked(db, admin.id)
    _check_lock(factor, moment)
    token, ident = secrets.token_urlsafe(32), str(uuid.uuid4())
    pending = None
    if not admin.mfa_enabled:
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        pending = secret_encryption.encrypt(secret.encode(), purpose="mfa-enrollment:" + ident).decode()
    db.add(MfaChallenge(id=ident, admin_id=admin.id, token_hash=_hash(token), session_version=admin.session_version or 0,
                         password_fingerprint=security._password_fingerprint(admin), pending_secret=pending,
                         expires_at=moment + dt.timedelta(minutes=5)))
    _audit(db, admin.id, "mfa.challenge.create")
    db.commit()
    return {"mfa_required": True, "challenge": token, "enrollment_required": not admin.mfa_enabled, "expires_in": 300}


def _challenge(db: Session, token: str, now: dt.datetime) -> tuple[AdminUser, AdminMfa, MfaChallenge]:
    found = db.scalar(select(MfaChallenge).where(MfaChallenge.token_hash == _hash(token)))
    if found is None:
        raise HTTPException(401, "Invalid or expired MFA challenge")
    admin, factor = _locked(db, found.admin_id)
    challenge = db.scalar(select(MfaChallenge).where(MfaChallenge.id == found.id).with_for_update())
    if (challenge.consumed_at is not None or _utc(challenge.expires_at) <= now
            or challenge.session_version != (admin.session_version or 0)
            or not hmac.compare_digest(challenge.password_fingerprint, security._password_fingerprint(admin))):
        raise HTTPException(401, "Invalid or expired MFA challenge")
    _check_lock(factor, now)
    return admin, factor, challenge


def enrollment(db: Session, token: str, now: dt.datetime | None = None) -> dict:
    admin, _, challenge = _challenge(db, token, _now(now))
    if admin.mfa_enabled or not challenge.pending_secret:
        raise HTTPException(409, "MFA sudah terdaftar. Gunakan kode dari authenticator.")
    secret = secret_encryption.decrypt(challenge.pending_secret.encode(), purpose="mfa-enrollment:" + challenge.id).decode()
    uri = "otpauth://totp/" + quote("SAVEGEO:" + admin.username, safe="") + "?" + urlencode({"secret": secret, "issuer": "SAVEGEO", "algorithm": "SHA1", "digits": 6, "period": 30})
    db.rollback()
    return {"secret": secret, "provisioning_uri": uri}


def _bad_code(db: Session, factor: AdminMfa, now: dt.datetime) -> NoReturn:
    factor.failures += 1
    if factor.failures >= 5:
        factor.locked_until = now + dt.timedelta(minutes=5)
    _audit(db, factor.admin_id, "mfa.verify.failed")
    db.commit()
    raise HTTPException(401, "Kode MFA tidak valid atau sudah digunakan.")


def _verify_factor(db: Session, factor: AdminMfa, value: str, now: dt.datetime) -> None:
    _check_lock(factor, now)
    if not factor.secret_ciphertext:
        raise HTTPException(409, "MFA belum terdaftar.")
    secret = secret_encryption.decrypt(factor.secret_ciphertext.encode(), purpose=f"admin-mfa:{factor.admin_id}").decode()
    counter = totp.matching_counter(secret, value, int(now.timestamp()), factor.last_counter)
    if counter is not None:
        factor.last_counter = counter
    else:
        hashed = _hash(value.replace("-", "").lower())
        matched = next((item for item in factor.recovery_hashes if hmac.compare_digest(item, hashed)), None)
        if matched is None:
            _bad_code(db, factor, now)
        factor.recovery_hashes = [item for item in factor.recovery_hashes if item != matched]
    factor.failures, factor.locked_until = 0, None


def verify(db: Session, token: str, value: str, now: dt.datetime | None = None) -> tuple[AdminUser, list[str]]:
    moment = _now(now)
    admin, factor, challenge = _challenge(db, token, moment)
    recovery = []
    if not admin.mfa_enabled:
        if not challenge.pending_secret:
            raise HTTPException(409, "Enrollment diperlukan.")
        secret = secret_encryption.decrypt(challenge.pending_secret.encode(), purpose="mfa-enrollment:" + challenge.id).decode()
        counter = totp.matching_counter(secret, value, int(moment.timestamp()))
        if counter is None:
            _bad_code(db, factor, moment)
        factor.secret_ciphertext = secret_encryption.encrypt(secret.encode(), purpose=f"admin-mfa:{admin.id}").decode()
        recovery = [secrets.token_hex(10) for _ in range(10)]
        factor.recovery_hashes = [_hash(item) for item in recovery]
        factor.last_counter, factor.enrolled_at = counter, moment
        factor.failures, factor.locked_until = 0, None
        admin.mfa_enabled = True
        admin.session_version = (admin.session_version or 0) + 1
        _audit(db, admin.id, "mfa.enroll")
    else:
        _verify_factor(db, factor, value, moment)
    challenge.pending_secret, challenge.consumed_at = None, moment
    admin.last_login = moment
    _audit(db, admin.id, "mfa.login.verified")
    db.commit()
    return admin, recovery


def step_up(db: Session, admin: AdminUser, value: str, now: dt.datetime | None = None) -> AdminUser:
    moment = _now(now)
    admin, factor = _locked(db, admin.id)
    _verify_factor(db, factor, value, moment)
    _audit(db, admin.id, "mfa.step_up")
    db.commit()
    return admin


def reset(db: Session, admin: AdminUser, password: str, value: str, now: dt.datetime | None = None) -> None:
    moment = _now(now)
    admin, factor = _locked(db, admin.id)
    if not security.verify_password(password, admin.password_hash):
        _bad_code(db, factor, moment)
    _verify_factor(db, factor, value, moment)
    factor.secret_ciphertext, factor.recovery_hashes = None, []
    factor.enrolled_at, factor.last_counter = None, -1
    admin.mfa_enabled = False
    admin.session_version = (admin.session_version or 0) + 1
    _audit(db, admin.id, "mfa.reset.self")
    db.commit()


def regenerate_recovery(db: Session, admin: AdminUser, password: str, value: str, now: dt.datetime | None = None) -> list[str]:
    moment = _now(now)
    admin, factor = _locked(db, admin.id)
    if not security.verify_password(password, admin.password_hash):
        _bad_code(db, factor, moment)
    _verify_factor(db, factor, value, moment)
    codes = [secrets.token_hex(10) for _ in range(10)]
    factor.recovery_hashes = [_hash(code) for code in codes]
    _audit(db, admin.id, "mfa.recovery.regenerate")
    db.commit()
    return codes
