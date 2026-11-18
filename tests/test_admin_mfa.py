import base64
import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core import security, secret_encryption, totp
from app.core.config import get_settings
from app.db.models.admin_mfa import AdminMfa
from app.db.models.admin_user import AdminUser
from app.db.models.audit_log import AuditLog
from sqlalchemy import select
from app.services import admin_mfa
from app.schemas.admin import AdminUserUpdateRequest
from app.api.routes.admin import admin_user_update

pytest_plugins = ["tests.test_organizations"]
NOW = dt.datetime(2026, 11, 13, 10, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    settings = get_settings().model_copy(update={"app_env": "development", "mfa_require_privileged_admins": None,
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
        account = db.get(AdminUser, 1)
        account.password_hash = security.hash_password("test-password-only")
        account.mfa_required = True
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
        account.mfa_required = True
        db.commit()
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


def test_per_account_mfa_requirement_defaults_off_even_in_production(env, monkeypatch):
    _, factory, _ = env
    production_settings = get_settings().model_copy(update={"app_env": "production", "mfa_require_privileged_admins": None})
    assert not production_settings.privileged_mfa_required
    monkeypatch.setattr(security, "get_settings", lambda: production_settings)
    with factory() as db:
        optional_account = AdminUser(
            id=3,
            username="read-only-reviewer",
            password_hash="x",
            is_superuser=False,
            mfa_enabled=False,
            mfa_required=False,
        )
        db.add(optional_account)
        db.commit()
        assert not security.requires_admin_mfa(optional_account)

        optional_account.mfa_required = True
        db.flush()
        assert security.requires_admin_mfa(optional_account)
        optional_account.mfa_required = False
        optional_account.mfa_enabled = True
        db.flush()
        assert not security.requires_admin_mfa(optional_account)
        optional_account.mfa_required = None
        optional_account.mfa_enabled = True
        db.flush()
        optional_account.is_superuser = True
        db.flush()
        assert not security.requires_admin_mfa(optional_account)
        forced_settings = production_settings.model_copy(update={"mfa_require_privileged_admins": True})
        monkeypatch.setattr(security, "get_settings", lambda: forced_settings)
        assert security.requires_admin_mfa(optional_account)


def test_admin_user_mfa_requirement_update_is_audited(env):
    _, factory, _ = env
    with factory() as db:
        actor = db.get(AdminUser, 1)
        target = AdminUser(
            id=3,
            username="read-only-reviewer",
            password_hash="x",
            is_superuser=False,
            mfa_enabled=False,
            mfa_required=False,
        )
        db.add(target)
        db.commit()
        actor._mfa_verified_at = int(dt.datetime.now(dt.UTC).timestamp())
        stale_target_session = security.create_access_token(target)

        response = admin_user_update(
            user_id=target.id,
            payload=AdminUserUpdateRequest(mfa_required=True),
            admin=actor,
            db=db,
        )

        assert response["user"]["mfa_required"] is True
        assert target.session_version == 1
        with pytest.raises(HTTPException):
            security.ensure_current_session(security.decode_access_token(stale_target_session), target)
        event = db.scalar(select(AuditLog).where(
            AuditLog.action == "user.update",
            AuditLog.resource_type == "admin_user",
            AuditLog.resource_id == "3",
        ))
        assert event.detail["mfa_required"] is True


def test_only_superuser_can_change_an_account_mfa_requirement(env):
    _, factory, _ = env
    with factory() as db:
        actor = AdminUser(
            id=4,
            username="non-superuser-editor",
            password_hash="x",
            is_superuser=False,
            mfa_enabled=False,
            mfa_required=False,
        )
        target = AdminUser(
            id=3,
            username="mfa-target",
            password_hash="x",
            is_superuser=False,
            mfa_enabled=False,
            mfa_required=False,
        )
        db.add_all([actor, target])
        db.commit()

        with pytest.raises(HTTPException) as error:
            admin_user_update(
                user_id=target.id,
                payload=AdminUserUpdateRequest(mfa_required=True),
                admin=actor,
                db=db,
            )
        assert error.value.status_code == 403


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


def test_operator_reset_revokes_target_and_creates_private_security_notice(env):
    client, factory, _ = env
    _, recovery, _ = enroll(factory)
    with factory() as db:
        actor = db.get(AdminUser, 1)
        actor.password_hash = security.hash_password("operator-test-password")
        target = db.get(AdminUser, 2)
        target.mfa_enabled = True
        target.session_version = 7
        db.commit()
        stale = security.create_access_token(target, mfa_verified_at=int(dt.datetime.now(dt.UTC).timestamp()))
        admin_mfa.reset_other(db, actor, "reviewer", "operator-test-password", recovery[0], "Identity confirmed by project operator", NOW)
        assert target.session_version == 8 and not target.mfa_enabled
        # Resetting an authenticator does not enable a requirement by itself.
        assert not security.requires_admin_mfa(target)
        # A stale authenticated cookie cannot retrieve target data after reset.
        assert client.get("/admin/auth/mfa/events", headers={"Authorization": "Bearer " + stale}).status_code == 401
        notice = db.scalar(select(AuditLog).where(AuditLog.action == "mfa.reset.operator", AuditLog.resource_id == "2"))
        assert notice.detail["reason"] == "Identity confirmed by project operator"
        actor_token = security.create_access_token(actor, mfa_verified_at=int(dt.datetime.now(dt.UTC).timestamp()))
        own_events = client.get("/admin/auth/mfa/events", headers={"Authorization": "Bearer " + actor_token})
        assert own_events.status_code == 200
        assert not any(event["action"] == "mfa.reset.operator" for event in own_events.json())
        with pytest.raises(HTTPException):
            admin_mfa.reset_other(db, actor, "root-admin", "operator-test-password", recovery[1], "Must use self reset instead", NOW)

