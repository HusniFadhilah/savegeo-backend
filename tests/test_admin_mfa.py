import base64
import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select

from app.core import security, secret_encryption, totp
from app.core.config import get_settings
from app.db.models.admin_mfa import AdminMfa
from app.db.models.admin_user import AdminUser
from app.services import admin_mfa

pytest_plugins = ["tests.test_organizations"]
NOW = dt.datetime(2026, 11, 13, 10, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    settings = get_settings().model_copy(update={"mfa_require_privileged_admins": True,
        "secret_encryption_active_key_id": "test-v1",
        "secret_encryption_keys": SecretStr(json.dumps({"test-v1": base64.b64encode(b"a" * 32).decode()}))})
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    monkeypatch.setattr(secret_encryption, "get_settings", lambda: settings)


@pytest.mark.parametrize("timestamp,expected", [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")])
def test_rfc_6238_sha1_vectors(timestamp, expected):
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp.code(secret, timestamp, digits=8) == expected


def enroll(factory, moment=NOW):
    with factory() as db:
        account = db.get(AdminUser, 1)
        challenge = admin_mfa.begin(db, account, moment)
        secret = admin_mfa.enrollment(db, challenge["challenge"], moment)["secret"]
        account, recovery = admin_mfa.verify(db, challenge["challenge"], totp.code(secret, int(moment.timestamp())), moment)
        return secret, recovery, account.session_version


def test_enrollment_ciphertext_recovery_replay_and_reset(env):
    _, factory, _ = env
    secret, recovery, version = enroll(factory)
    with factory() as db:
        account = db.get(AdminUser, 1)
        factor = db.get(AdminMfa, 1)
        assert account.mfa_enabled and version == 1
        assert secret not in factor.secret_ciphertext
        assert not any(code in json.dumps(factor.recovery_hashes) for code in recovery)
        challenge = admin_mfa.begin(db, account, NOW)
        account, codes = admin_mfa.verify(db, challenge["challenge"], recovery[0], NOW)
        assert codes == [] and len(db.get(AdminMfa, 1).recovery_hashes) == 9
        with pytest.raises(HTTPException):
            admin_mfa.verify(db, challenge["challenge"], recovery[0], NOW)
        db.rollback()
        challenge2 = admin_mfa.begin(db, account, NOW)
        with pytest.raises(HTTPException):
            admin_mfa.verify(db, challenge2["challenge"], recovery[0], NOW)
        db.rollback()
        with pytest.raises(HTTPException):
            admin_mfa.verify(db, challenge2["challenge"], totp.code(secret, int(NOW.timestamp())), NOW)


def test_expiry_password_change_and_global_attempt_limit(env):
    _, factory, _ = env
    secret, _, _ = enroll(factory)
    with factory() as db:
        account = db.get(AdminUser, 1)
        pending = admin_mfa.begin(db, account, NOW)
        with pytest.raises(HTTPException):
            admin_mfa.verify(db, pending["challenge"], "000000", NOW + dt.timedelta(minutes=6))
        db.rollback()
        account.password_hash = "changed-after-challenge"
        db.commit()
        with pytest.raises(HTTPException):
            admin_mfa.verify(db, pending["challenge"], totp.code(secret, int(NOW.timestamp())), NOW)
        db.rollback()
        pending = admin_mfa.begin(db, account, NOW)
        for _ in range(5):
            with pytest.raises(HTTPException) as error:
                admin_mfa.verify(db, pending["challenge"], "xxxxxx", NOW)
            assert error.value.status_code == 401
        with pytest.raises(HTTPException) as error:
            admin_mfa.begin(db, account, NOW)
        assert error.value.status_code == 429


def test_login_never_issues_full_session_before_mfa(env):
    client, factory, _ = env
    with factory() as db:
        db.get(AdminUser, 1).password_hash = security.hash_password("test-password-only")
        db.commit()
    response = client.post("/admin/auth/login", json={"username": "root-admin", "password": "test-password-only"})
    assert response.status_code == 200, response.text
    assert response.json()["mfa_required"]
    assert client.get("/admin/auth/me").status_code == 401
    token = response.json()["challenge"]
    setup = client.post("/admin/auth/mfa/enrollment", json={"challenge": token})
    assert setup.status_code == 200, setup.text
    assert setup.headers["cache-control"] == "no-store"
    secret = setup.json()["secret"]
    code = totp.code(secret, int(dt.datetime.now(dt.UTC).timestamp()))
    verified = client.post("/admin/auth/mfa/verify", json={"challenge": token, "code": code})
    assert verified.status_code == 200, verified.text
    assert len(verified.json()["recovery_codes"]) == 10
    assert client.get("/admin/auth/me").status_code == 200
    assert client.post("/admin/auth/mfa/verify", json={"challenge": token, "code": code}).status_code == 401
    recovery = verified.json()["recovery_codes"]
    reset = client.post("/admin/auth/mfa/reset", json={"password": "test-password-only", "code": recovery[0]})
    assert reset.status_code == 200, reset.text
    assert client.get("/admin/auth/me").status_code == 401
    response = client.post("/admin/auth/login", json={"username": "root-admin", "password": "test-password-only"})
    assert response.json()["enrollment_required"]


def test_required_policy_rejects_legacy_token_and_step_up_expires(env):
    _, factory, _ = env
    with factory() as db:
        account = db.get(AdminUser, 1)
        payload = security.decode_access_token(security.create_access_token(account))
        with pytest.raises(HTTPException):
            security.ensure_admin_mfa(payload, account)
    enroll(factory)
    with factory() as db:
        account = db.get(AdminUser, 1)
        now = int(dt.datetime.now(dt.UTC).timestamp())
        payload = security.decode_access_token(security.create_access_token(account, mfa_verified_at=now - 301))
        security.ensure_admin_mfa(payload, account)
        with pytest.raises(HTTPException) as error:
            security.require_recent_mfa(account)
        assert error.value.detail["code"] == "mfa_step_up_required"


def test_concurrent_recovery_code_can_only_be_used_once(env):
    _, factory, dialect = env
    if dialect != "postgresql":
        pytest.skip("Concurrent locks require PostgreSQL")
    _, recovery, _ = enroll(factory)
    with factory() as db:
        account = db.get(AdminUser, 1)
        tokens = [admin_mfa.begin(db, account, NOW)["challenge"] for _ in range(2)]
    barrier = threading.Barrier(2)
    def verify(token):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                admin_mfa.verify(db, token, recovery[0], NOW)
                return "ok"
            except HTTPException:
                return "denied"
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(verify, token) for token in tokens]
        assert sorted(future.result(timeout=20) for future in futures) == ["denied", "ok"]
