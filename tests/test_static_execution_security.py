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
from app.providers import local_raster_provider
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


def test_gdal_provider_guard_shares_public_provider_circuit_breaker(monkeypatch):
    outcomes = []
    monkeypatch.setattr(public_http, "public_addresses", lambda _host: ["93.184.215.14"])
    monkeypatch.setattr(public_http.public_http, "_circuit_before", lambda host: outcomes.append(("before", host)))
    monkeypatch.setattr(
        public_http.public_http,
        "_circuit_result",
        lambda host, *, success: outcomes.append(("result", host, success)),
    )

    with local_raster_provider.raster_provider_guard("assets.example"):
        pass
    with pytest.raises(RuntimeError):
        with local_raster_provider.raster_provider_guard("assets.example"):
            raise RuntimeError("upstream read failed")

    assert outcomes == [
        ("before", "assets.example"), ("result", "assets.example", True),
        ("before", "assets.example"), ("result", "assets.example", False),
    ]
    assert local_raster_provider.GDAL_HTTP_OPTS["GDAL_HTTP_MAX_RETRY"] == 0


def test_gdal_remote_raster_requires_bounded_non_redirecting_range_probe(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "assets.example", ["93.184.215.14"]),
    )

    def request(method, url, **kwargs):
        calls.update(method=method, url=url, **kwargs)
        return SimpleNamespace(status_code=206, headers={"Content-Range": "bytes 0-0/1048576"})

    monkeypatch.setattr(public_http.public_http, "request", request)
    hostname = local_raster_provider.validate_remote_raster_url(
        "https://assets.example/plot.tif?sig=private",
        {"GDAL_HTTP_HEADERS": "Authorization: Bearer private\nX-Debug: do-not-forward"},
    )

    assert hostname == "assets.example"
    assert calls["method"] == "GET"
    assert calls["headers"] == {"Range": "bytes=0-0", "Authorization": "Bearer private"}
    assert calls["max_bytes"] == 1
    assert calls["timeout"] == (3, 8)
    assert calls["allow_redirects"] is False


@pytest.mark.parametrize(
    "status,content_range",
    [
        (302, ""),
        (200, ""),
        (206, ""),
        (206, "bytes 0-0/536870913"),
    ],
)
def test_gdal_remote_raster_rejects_redirects_unknown_sizes_and_oversized_files(
    monkeypatch, status, content_range
):
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "assets.example", ["93.184.215.14"]),
    )
    monkeypatch.setattr(
        public_http.public_http,
        "request",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=status, headers={"Content-Range": content_range}
        ),
    )

    with pytest.raises(ValueError, match="bounded public HTTPS range check"):
        local_raster_provider.validate_remote_raster_url("https://assets.example/plot.tif")


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


def test_public_https_retries_safe_get_and_opens_provider_circuit(monkeypatch):
    hostname = "retry-circuit.example"
    monkeypatch.setattr(public_http, "public_addresses", lambda _host: ["93.184.215.14"])
    calls = {"count": 0}

    class Pool:
        def __init__(self, *_args, **_kwargs):
            pass

        def urlopen(self, *_args, **_kwargs):
            calls["count"] += 1
            raise public_http.urllib3.exceptions.ProtocolError("temporary connection failure")

        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, "HTTPSConnectionPool", Pool)
    public_http._CIRCUITS.pop(hostname, None)
    monkeypatch.setitem(public_http._CIRCUITS, hostname, {"failures": 0, "opened_until": 0.0, "probe_in_flight": False})
    for _ in range(3):
        with pytest.raises(public_http.requests.RequestException):
            public_http.public_http.get(f"https://{hostname}/data", timeout=3)
    before_open_circuit = calls["count"]
    with pytest.raises(public_http.requests.ConnectionError, match="circuit is open"):
        public_http.public_http.get(f"https://{hostname}/data", timeout=3)
    assert calls["count"] == before_open_circuit


def test_public_https_json_post_is_serialized_and_bounded(monkeypatch):
    calls = {}
    monkeypatch.setattr(public_http, 'public_addresses', lambda _: ['93.184.215.14'])

    class Pool:
        def __init__(self, *args, **kwargs): pass
        def urlopen(self, method, target, **kwargs):
            calls.update(method=method, target=target, kwargs=kwargs)
            stream = io.BytesIO(b'{"features":[]}')
            raw = SimpleNamespace(read1=lambda size: stream.read(size))
            return SimpleNamespace(status=200, headers={'Content-Type': 'application/json'}, _fp=raw, close=lambda: None)
        def close(self): pass

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    response = public_http.public_http.post(
        'https://example.com/search', json_body={'limit': 5}, max_bytes=64
    )
    assert calls['method'] == 'POST'
    assert calls['kwargs']['body'] == b'{"limit":5}'
    assert calls['kwargs']['headers']['Content-Type'] == 'application/json'
    assert response.json() == {'features': []}


def test_public_https_response_cap_and_close(monkeypatch):
    calls = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda _: ['93.184.215.14'])
    class Pool:
        def __init__(self, *a, **k): pass
        def urlopen(self, *a, **k):
            stream = io.BytesIO(b'x' * 32)
            raw = SimpleNamespace(read1=lambda n: stream.read(n))
            return SimpleNamespace(status=200, headers={}, _fp=raw, close=lambda: calls.append('response'))
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
            return SimpleNamespace(status=200, headers={}, _fp=SlowResponse(), close=lambda: None)
        def close(self):
            return None

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    started = time.monotonic()
    with pytest.raises(Exception, match='deadline|timed out|timeout'):
        public_http.public_http.get('https://example.com/', timeout=0.06)
    assert time.monotonic() - started < 0.3


def test_public_https_rejects_unexpected_content_encoding(monkeypatch):
    monkeypatch.setattr(public_http, 'public_addresses', lambda _: ['93.184.215.14'])

    class Pool:
        def __init__(self, *args, **kwargs): pass
        def urlopen(self, *args, **kwargs):
            return SimpleNamespace(status=200, headers={'Content-Encoding': 'gzip'}, close=lambda: None)
        def close(self): pass

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    with pytest.raises(ValueError, match='Encoded public HTTPS'):
        public_http.public_http.get('https://example.com/')


def test_public_https_download_pins_each_allowlisted_redirect_and_streams_to_file(monkeypatch, tmp_path):
    resolved = []
    requests_seen = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda host: resolved.append(host) or ['93.184.215.14'])
    payload = b'bounded-model-asset'

    class Pool:
        def __init__(self, address, **kwargs):
            requests_seen.append(('pool', kwargs['server_hostname'], address))
            self.hostname = kwargs['server_hostname']
        def urlopen(self, method, target, **kwargs):
            requests_seen.append(('request', self.hostname, method, kwargs['headers']))
            if self.hostname == 'huggingface.co':
                return SimpleNamespace(status=302, headers={'Location': 'https://cas-bridge.xethub.hf.co/asset?signature=secret'}, close=lambda: None)
            stream = io.BytesIO(payload)
            raw = SimpleNamespace(read1=lambda size: stream.read(size))
            return SimpleNamespace(status=200, headers={'Content-Length': str(len(payload))}, _fp=raw, close=lambda: None)
        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    target = tmp_path / 'asset.part'
    response = public_http.public_http.download_to_file(
        'https://huggingface.co/org/model/resolve/revision/config.json', target,
        max_bytes=128, allowed_redirect_hosts={'huggingface.co', 'xethub.hf.co'},
        headers={'User-Agent': 'SaveGeo/test'},
    )
    assert response.status_code == 200
    assert target.read_bytes() == payload
    assert resolved == ['huggingface.co', 'cas-bridge.xethub.hf.co']
    assert all('Authorization' not in item[3] for item in requests_seen if item[0] == 'request')


def test_public_https_download_blocks_unapproved_redirect_before_dns(monkeypatch, tmp_path):
    resolved = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda host: resolved.append(host) or ['93.184.215.14'])

    class Pool:
        def __init__(self, *args, **kwargs):
            pass
        def urlopen(self, *args, **kwargs):
            return SimpleNamespace(status=302, headers={'Location': 'https://169.254.169.254/latest/meta-data/'}, close=lambda: None)
        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    target = tmp_path / 'asset.part'
    with pytest.raises(ValueError, match='redirect destination is not allowed'):
        public_http.public_http.download_to_file(
            'https://huggingface.co/org/model/resolve/revision/weights.bin', target,
            max_bytes=128, allowed_redirect_hosts={'huggingface.co', 'xethub.hf.co'},
        )
    assert resolved == ['huggingface.co']
    assert not target.exists()


def test_public_https_stream_pins_each_redirect_and_strips_authentication(monkeypatch):
    resolved = []
    seen = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda host: resolved.append(host) or ['93.184.215.14'])
    monkeypatch.setattr(public_http.public_http, '_circuit_before', lambda _host: None)
    monkeypatch.setattr(public_http.public_http, '_circuit_result', lambda *_args, **_kwargs: None)
    payload = b'streamed-raster-bytes'

    class Pool:
        def __init__(self, address, **kwargs):
            self.hostname = kwargs['server_hostname']
            assert address == '93.184.215.14'
        def urlopen(self, method, target, **kwargs):
            seen.append((self.hostname, method, kwargs['headers']))
            if self.hostname == 'catalog.example':
                return SimpleNamespace(status=302, headers={'Location': 'https://assets.example/data.tif'}, close=lambda: None)
            stream = io.BytesIO(payload)
            raw = SimpleNamespace(read1=lambda size: stream.read(size))
            return SimpleNamespace(status=200, headers={'Content-Length': str(len(payload))}, _fp=raw, close=lambda: None)
        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, 'HTTPSConnectionPool', Pool)
    response = public_http.public_http.open_stream(
        'https://catalog.example/data.tif', max_bytes=128,
        allowed_redirect_hosts={'catalog.example', 'assets.example'},
        headers={'Authorization': 'Bearer private'}, timeout=10,
    )
    assert b''.join(response.iter_bytes()) == payload
    assert resolved == ['catalog.example', 'assets.example']
    assert seen[0][2]['Authorization'] == 'Bearer private'
    assert 'Authorization' not in seen[1][2]


def test_gdal_raster_path_rejects_private_dns_before_open_and_redacts_signed_href(monkeypatch, caplog):
    import rasterio

    monkeypatch.setattr(public_http, "public_addresses", lambda _host: ["127.0.0.1"])
    monkeypatch.setattr(rasterio, "open", lambda *_a, **_k: pytest.fail("GDAL open must not run"))

    result = local_raster_provider.load_raster_to_grid(
        "https://raster.example/data.tif?token=secret-value",
        object(),
    )

    assert result is None
    assert "secret-value" not in caplog.text
    assert "127.0.0.1" not in caplog.text


def test_imagery_asset_url_uses_the_same_public_dns_policy(monkeypatch):
    from app.services import imagery_service
    from app.services.gee_common import AnalysisError

    monkeypatch.setattr(public_http, "public_addresses", lambda _host: ["169.254.169.254"])
    with pytest.raises(AnalysisError, match="HTTPS"):
        imagery_service._validate_public_http_url("https://raster.example/item.tif")


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
    pytest.importorskip("skl2onnx")
    pytest.importorskip("onnxruntime")
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
    pytest.importorskip("onnx")
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
