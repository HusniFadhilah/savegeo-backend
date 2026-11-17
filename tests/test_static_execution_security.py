"""Behavioral regressions for the current signed-review remediation run."""
from __future__ import annotations

import io
import socket
import time
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core import public_http
from app.api.routes import models, workflows
from app.db.models.uploaded_model import UploadedModel
from app.db.models.workflow import Workflow
from app.db.models.user import User
from app.inference.carbon_model import CarbonEstimationModel
from app.inference.safe_model import validate_onnx
from app.providers.feature_engineering_non_gee import compute_terrain, compute_interactions


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fe80::1', '::ffff:127.0.0.1', '100.64.0.1'])
def test_public_https_rejects_private_dns_before_connect(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', (address, 443))])
    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', lambda *a, **k: pytest.fail('Connected before rejection'))
    with pytest.raises(ValueError):
        public_http.public_http.get('https://allowed.arcgis.com/path')


def test_public_https_pins_address_with_original_sni_and_no_redirect(monkeypatch):
    calls = {}
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('93.184.215.14', 443))])
    class Pool:
        def __init__(self, address, **kwargs):
            calls.update(address=address, **kwargs)
        def urlopen(self, method, target, **kwargs):
            calls.update(method=method, target=target, request=kwargs)
            return SimpleNamespace(status=302, headers={'Location': 'http://127.0.0.1'}, close=lambda: None)
        def close(self):
            calls['closed'] = True
    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    with pytest.raises(ValueError, match='redirect'):
        public_http.public_http.get('https://example.arcgis.com/data', params={'token': 'dummy-test'})
    assert calls['address'] == '93.184.215.14'
    assert calls['server_hostname'] == calls['assert_hostname'] == 'example.arcgis.com'
    assert calls['request']['headers']['Host'] == 'example.arcgis.com'
    assert calls['request']['redirect'] is False and calls['request']['retries'] is False
    assert calls['closed']


def test_public_https_response_cap_and_close(monkeypatch):
    calls = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda _: ['93.184.215.14'])
    class Pool:
        def __init__(self, *a, **k): pass
        def urlopen(self, *a, **k):
            stream = io.BytesIO(b'x' * 32)
            return SimpleNamespace(status=200, headers={}, read1=lambda n, **k: stream.read(n), close=lambda: calls.append('response'))
        def close(self): calls.append('pool')
    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    with pytest.raises(ValueError, match='size limit'):
        public_http.public_http.get('https://example.com/', max_bytes=8)
    assert calls == ['response', 'pool']


def test_public_https_enforces_total_deadline_on_a_slow_stream(monkeypatch):
    monkeypatch.setattr(public_http, 'public_addresses', lambda _: ['93.184.215.14'])

    class SlowResponse:
        status = 200
        headers = {}
        def __init__(self):
            self.reads = 0
        def read1(self, _size, **_kwargs):
            self.reads += 1
            time.sleep(0.025)
            return b'x' if self.reads <= 10 else b''
        def close(self):
            return None

    class Pool:
        def __init__(self, *args, **kwargs):
            return None
        def urlopen(self, *args, **kwargs):
            return SlowResponse()
        def close(self):
            return None

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    started = time.monotonic()
    with pytest.raises(Exception, match='deadline|timed out|timeout'):
        public_http.public_http.get('https://example.com/', timeout=0.06)
    assert time.monotonic() - started < 0.3


def test_public_model_metadata_is_allowlisted():
    row = UploadedModel(name='carbon', filename='private.pkl', filepath='/private/model',
                        metadata_json={'gee_deployable': True, 'private_key': 'do-not-echo', 'bucket_path': '/private', 'trained_at': '2026-01-01'})
    result = row.to_dict_public(include_path=True)
    assert 'filename' not in result and 'filepath' not in result and 'uploaded_by' not in result
    assert result['metadata_json'] == {'gee_deployable': True, 'trained_at': '2026-01-01'}
    with pytest.raises(HTTPException) as error:
        models.models_list(include_inactive=True, db=None)
    assert error.value.status_code == 403


def test_private_workflow_hides_existence_and_share_recomputes_checksum(monkeypatch):
    row = Workflow(id=10, owner_id=1, name='Private', visibility='private', version=1, workflow_json={'visibility': 'private'})
    with pytest.raises(HTTPException) as error:
        workflows._public_or_owner(row, User(id=2))
    assert error.value.status_code == 404
    monkeypatch.setattr(workflows, '_find_workflow', lambda *a: row)
    monkeypatch.setattr(workflows, 'ensure_workflow_feature_access', lambda *a: None)
    workflows.share_workflow('10', User(id=1), SimpleNamespace(commit=lambda: None))
    assert row.visibility == 'unlisted' and row.version == 2
    assert row.checksum == workflows._checksum(row.workflow_json)


def test_optional_workflow_auth_rejects_malformed_header():
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [(b'authorization', b'Basic abc')]})
    with pytest.raises(HTTPException) as error:
        workflows.optional_user(request, None, None)
    assert error.value.status_code == 401


def test_workflow_execution_does_not_fabricate_success_or_write_run(monkeypatch):
    from app.schemas.workflow import WorkflowRunRequest
    row = Workflow(id=10, owner_id=1, name='Saved definition', workflow_json={'nodes': [{'id': 'a'}]})
    monkeypatch.setattr(workflows, '_find_workflow', lambda *args: row)
    monkeypatch.setattr(workflows, 'ensure_workflow_feature_access', lambda *args: None)
    db = SimpleNamespace(add=lambda *args: pytest.fail('No execution engine must create no run'), commit=lambda: pytest.fail('No data may be committed'))
    with pytest.raises(HTTPException) as error:
        workflows.run_workflow('10', WorkflowRunRequest(), User(id=1), db)
    assert error.value.status_code == 501


@pytest.mark.parametrize('algorithm', ['linear', 'ridge', 'random_forest', 'hist_gradient_boosting', 'lightgbm'])
def test_onnx_roundtrip_preserves_predictions_and_never_loads_pickle(tmp_path, monkeypatch, algorithm):
    import joblib
    rng = np.random.default_rng(5)
    X = rng.normal(size=(60, 3))
    y = X @ np.array([2., 3., 4.]) + .1
    model = CarbonEstimationModel(algorithm, **({'random_state': 42} if algorithm not in {'linear', 'ridge'} else {}))
    model.train(X, y, ['a', 'b', 'c'], cv_folds=3)
    original = model.predict(X)
    monkeypatch.setattr(joblib, 'load', lambda *a, **k: pytest.fail('Unsafe pickle loader was invoked'))
    path = tmp_path / 'model.pkl'
    model.save(str(path))
    assert not path.exists() and path.with_suffix('.onnx').exists()
    loaded = CarbonEstimationModel.load(str(path))
    np.testing.assert_allclose(loaded.predict(X), original, rtol=1e-4, atol=1e-3)


def test_legacy_pickle_is_not_read(tmp_path, monkeypatch):
    import joblib
    path = tmp_path / 'hostile.pkl'
    path.write_bytes(b'not-a-safe-model')
    monkeypatch.setattr(joblib, 'load', lambda *a, **k: pytest.fail('Unsafe pickle loader was invoked'))
    with pytest.raises(ValueError, match='ONNX'):
        CarbonEstimationModel.load(str(path))


def test_onnx_rejects_external_data_and_custom_operator():
    from onnx import helper, TensorProto
    graph = helper.make_graph([helper.make_node('Exploit', ['X'], ['Y'], domain='untrusted.code')], 'bad',
        [helper.make_tensor_value_info('X', TensorProto.FLOAT, [None, 1])],
        [helper.make_tensor_value_info('Y', TensorProto.FLOAT, [None, 1])])
    with pytest.raises(ValueError, match='Custom'):
        validate_onnx(helper.make_model(graph).SerializeToString())
    tensor = helper.make_tensor('weights', TensorProto.FLOAT, [1], [1.])
    tensor.data_location = TensorProto.EXTERNAL
    item = tensor.external_data.add()
    item.key, item.value = 'location', '../../private'
    graph.initializer.append(tensor)
    with pytest.raises(ValueError, match='External'):
        validate_onnx(helper.make_model(graph).SerializeToString())


def test_terrain_missing_centre_and_neighbours_remain_missing():
    dem = np.zeros((7, 7), dtype=float)
    dem[3, 3] = np.nan
    result = compute_terrain(dem, 10)
    for key in ['slope', 'aspect']:
        assert np.isnan(result[key][3, 3])
        assert np.isnan(result[key][2, 3])
        assert result[key][0, 0] == 0
    with pytest.raises(ValueError):
        compute_terrain(dem, float('nan'))
    missing = compute_interactions({'B8': dem, 'B11': dem}, {}, None, None)
    assert np.isnan(missing['NDVI_x_elevation']).all()


def test_untrusted_weather_xml_rejects_entity_expansion():
    from app.services.disaster_service import _parse_bmkg_warning_xml
    from defusedxml.common import DefusedXmlException
    payload = '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///private/server-file">]><alert>&secret;</alert>'
    with pytest.raises(DefusedXmlException):
        _parse_bmkg_warning_xml(payload)
