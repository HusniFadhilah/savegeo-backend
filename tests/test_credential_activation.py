import datetime as dt

import pytest
from sqlalchemy import select

from app.db.models.gee_credential import GEECredential
from app.services import credential_activation as service, credential_probe

pytest_plugins = ["tests.test_organizations"]


def seed(factory):
    with factory() as db:
        for ident, active in ((1, True), (2, False)):
            db.add(GEECredential(id=ident, label=f"Credential {ident}", project_id="test-project",
                                client_email="fixture@example.test", json_filename="key.json", bucket_path=f"{ident}.json", is_active=active))
        db.commit()


def test_failed_probe_keeps_active_credential(env, monkeypatch):
    _, factory, _ = env
    seed(factory)
    monkeypatch.setattr(service.storage_service, "download_credential", lambda path: b"fixture")
    monkeypatch.setattr(credential_probe, "probe", lambda raw: credential_probe.ProbeResult(False, "permission_denied", dt.datetime.now(dt.UTC).isoformat()))
    def unexpected(db):
        pytest.fail("Failed candidate probe must not change global EE client")
    monkeypatch.setattr(service.gee_service, "initialize_ee", unexpected)
    with factory() as db:
        with pytest.raises(service.CredentialActivationError) as error:
            service.activate(db, 2, 1)
        assert error.value.code == "permission_denied"
        assert db.get(GEECredential, 1).is_active
        assert not db.get(GEECredential, 2).is_active
        assert db.get(GEECredential, 2).last_check_code == "permission_denied"


@pytest.mark.parametrize("runtime_success", [True, False])
def test_activation_restores_previous_choice_on_runtime_failure(env, monkeypatch, runtime_success):
    _, factory, _ = env
    seed(factory)
    monkeypatch.setattr(service.storage_service, "download_credential", lambda path: b"fixture")
    monkeypatch.setattr(credential_probe, "probe", lambda raw: credential_probe.ProbeResult(True, "ok", dt.datetime.now(dt.UTC).isoformat(), "test-project", "fixture@example.test"))
    outcomes = iter([runtime_success, True])
    monkeypatch.setattr(service.gee_service, "initialize_ee", lambda db: next(outcomes))
    with factory() as db:
        if runtime_success:
            assert service.activate(db, 2, 1)["gee_initialized"]
        else:
            with pytest.raises(service.CredentialActivationError) as error:
                service.activate(db, 2, 1)
            assert error.value.runtime_initialized is True
        db.expire_all()
        active = list(db.scalars(select(GEECredential.id).where(GEECredential.is_active.is_(True))))
        assert active == ([2] if runtime_success else [1])
