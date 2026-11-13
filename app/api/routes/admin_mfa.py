import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core import security, secret_encryption
from app.db.models.admin_user import AdminUser
from app.db.session import get_db
from app.services import admin_mfa

router = APIRouter(prefix="/admin/auth/mfa", tags=["admin-mfa"])


class ChallengeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge: str = Field(min_length=32, max_length=256)


class VerifyInput(ChallengeInput):
    code: str = Field(min_length=6, max_length=64)


class CodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=64)


class ResetInput(CodeInput):
    password: str = Field(min_length=1, max_length=1024)


def _unavailable():
    raise HTTPException(503, "Konfigurasi enkripsi MFA server belum tersedia. Hubungi administrator sistem.") from None


@router.post("/enrollment")
def enrollment(payload: ChallengeInput, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return admin_mfa.enrollment(db, payload.challenge)
    except secret_encryption.SecretEncryptionError:
        _unavailable()


@router.post("/verify")
def verify(payload: VerifyInput, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        account, recovery = admin_mfa.verify(db, payload.challenge, payload.code)
    except secret_encryption.SecretEncryptionError:
        _unavailable()
    timestamp = int(dt.datetime.now(dt.UTC).timestamp())
    security.set_session_cookie(response, security.ADMIN_SESSION_COOKIE, security.create_access_token(account, mfa_verified_at=timestamp))
    return {"user": account.to_dict(), "recovery_codes": recovery}


@router.post("/step-up")
def step_up(payload: CodeInput, response: Response, admin: AdminUser = Depends(security.get_current_admin), db: Session = Depends(get_db)):
    try:
        account = admin_mfa.step_up(db, admin, payload.code)
    except secret_encryption.SecretEncryptionError:
        _unavailable()
    security.set_session_cookie(response, security.ADMIN_SESSION_COOKIE,
                                security.create_access_token(account, mfa_verified_at=int(dt.datetime.now(dt.UTC).timestamp())))
    response.headers["Cache-Control"] = "no-store"
    return {"message": "MFA terverifikasi; tindakan sensitif tersedia selama lima menit."}


@router.post("/reset")
def reset(payload: ResetInput, response: Response, admin: AdminUser = Depends(security.get_current_admin), db: Session = Depends(get_db)):
    try:
        admin_mfa.reset(db, admin, payload.password, payload.code)
    except secret_encryption.SecretEncryptionError:
        _unavailable()
    security.clear_session_cookie(response, security.ADMIN_SESSION_COOKIE)
    return {"message": "MFA direset dan seluruh sesi dicabut. Masuk kembali untuk mendaftarkan authenticator baru."}


@router.post("/recovery-codes")
def recovery_codes(payload: ResetInput, response: Response, admin: AdminUser = Depends(security.get_current_admin), db: Session = Depends(get_db)):
    try:
        codes = admin_mfa.regenerate_recovery(db, admin, payload.password, payload.code)
    except secret_encryption.SecretEncryptionError:
        _unavailable()
    response.headers["Cache-Control"] = "no-store"
    return {"recovery_codes": codes}
