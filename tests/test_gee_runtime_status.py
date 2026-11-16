from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app.api.deps import require_ee
from app.api.routes import admin
from app.services import credential_activation


def request_with_state(initialized: bool) -> Request:
    return Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(ee_initialized=initialized))})


def assert_analysis_gate(request: Request, success: bool) -> None:
    if success:
        require_ee(request)
    else:
        with pytest.raises(HTTPException) as error:
            require_ee(request)
        assert error.value.status_code == 503


@pytest.mark.parametrize('success', [True, False])
def test_activate_credential_updates_analysis_gate(monkeypatch, success):
    request = request_with_state(not success)
    db = Mock()
    db.get.return_value = SimpleNamespace(is_active=False)
    initializer = Mock(return_value={"gee_initialized": True})
    if not success:
        initializer.side_effect = credential_activation.CredentialActivationError("permission_denied")
    monkeypatch.setattr(credential_activation, 'activate', initializer)
    monkeypatch.setattr(admin.audit_service, 'log_audit', Mock())

    if success:
        result = admin.activate_gee_credential(7, request, SimpleNamespace(id=1), db)
        assert result['gee_initialized'] is True
    else:
        with pytest.raises(HTTPException) as error:
            admin.activate_gee_credential(7, request, SimpleNamespace(id=1), db)
        assert error.value.status_code == 503
    initializer.assert_called_once_with(db, 7, 1)
    # A failed probe keeps the previously working runtime gate.
    assert request.app.state.ee_initialized is True


@pytest.mark.parametrize('activate,success', [(True, True), (True, False), (False, True)])
def test_upload_credential_updates_gate_only_when_activated(monkeypatch, activate, success):
    request = request_with_state(not success)
    db = Mock()
    initializer = Mock(return_value={"gee_initialized": True})
    if not success:
        initializer.side_effect = credential_activation.CredentialActivationError("permission_denied")
    monkeypatch.setattr(credential_activation, 'activate', initializer)
    monkeypatch.setattr(admin.storage_service, 'upload_credential', Mock())
    monkeypatch.setattr(admin.audit_service, 'log_audit', Mock())
    credential = SimpleNamespace(id=7, to_dict=lambda: {'id': 7})
    monkeypatch.setattr(admin, 'GEECredential', Mock(return_value=credential, is_active=Mock()))
    file = UploadFile(filename='test.json', file=io.BytesIO(json.dumps({
        'type': 'service_account', 'project_id': 'test-project',
        'private_key': 'test-only-placeholder', 'client_email': 'test@example.test',
    }).encode()))

    asyncio.run(admin.upload_gee_credential(
        request=request, file=file, label='Test', notes=None,
        activate=activate, admin=SimpleNamespace(id=1), db=db,
    ))

    if activate:
        initializer.assert_called_once_with(db, 7, 1)
        assert request.app.state.ee_initialized is True
    else:
        initializer.assert_not_called()
        assert request.app.state.ee_initialized is (not success)
