"""Behavioral regressions for the current signed-review remediation run."""
from __future__ import annotations

import io
import socket
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_remote_raster_url_requires_public_https_without_opening_a_connection(monkeypatch):
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "assets.example", ["93.184.215.14"]),
    )
    hostname = local_raster_provider.validate_remote_raster_url(
        "https://assets.example/plot.tif?sig=private",
    )
    assert hostname == "assets.example"
    with pytest.raises(ValueError, match="public HTTPS"):
        local_raster_provider.validate_remote_raster_url("ftp://assets.example/plot.tif")
    with pytest.raises(ValueError, match="virtual paths"):
        local_raster_provider.validate_remote_raster_url("/vsicurl/https://assets.example/plot.tif")


def test_safe_raster_source_serves_bounded_ranges_and_stops(monkeypatch):
    source_bytes = b"0123456789abcdef"
    calls = []
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "assets.example", ["93.184.215.14"]),
    )

    def get(url, *, headers, timeout, max_bytes):
        range_header = headers["Range"]
        calls.append((url, range_header, timeout, max_bytes, headers.copy()))
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        end = min(end, len(source_bytes) - 1)
        content = source_bytes[start:end + 1]
        return SimpleNamespace(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(source_bytes)}",
                "Content-Length": str(len(content)),
                "ETag": '"stable-version"',
            },
            content=content,
        )

    monkeypatch.setattr(public_http.public_http, "get", get)
    with local_raster_provider.safe_raster_source(
        "https://assets.example/plot.tif?sig=private",
        {"GDAL_HTTP_HEADERS": "Authorization: Bearer private"},
    ) as local_url:
        response = public_http.requests.get(local_url, headers={"Range": "bytes=2-5"}, timeout=3)
        assert response.status_code == 206
        assert response.content == source_bytes[2:6]
        assert response.headers["Content-Range"] == "bytes 2-5/16"
    assert len(calls) == 2
    assert calls[0][1] == "bytes=0-0" and calls[0][3] == 1
    assert calls[1][1] == "bytes=2-5" and calls[1][3] == 4
    assert calls[1][4]["Authorization"] == "Bearer private"
    with pytest.raises(public_http.requests.RequestException):
        public_http.requests.get(local_url, headers={"Range": "bytes=2-5"}, timeout=0.5)


def test_safe_raster_source_limits_range_bytes_and_rejects_unsupported_headers(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "bounded-range.example", ["93.184.215.14"]),
    )

    def get(_url, *, headers, **_kwargs):
        attempts.append(headers["Range"])
        object_size = local_raster_provider.MAX_REMOTE_RASTER_RANGE_BYTES + 2
        return SimpleNamespace(
            status_code=206,
            headers={"Content-Range": f"bytes 0-0/{object_size}"},
            content=b"x",
        )

    monkeypatch.setattr(public_http.public_http, "get", get)
    with local_raster_provider.safe_raster_source("https://bounded-range.example/plot.tif") as local_url:
        oversized = public_http.requests.get(
            local_url,
            headers={"Range": f"bytes=0-{local_raster_provider.MAX_REMOTE_RASTER_RANGE_BYTES}"},
            timeout=3,
        )
        assert oversized.status_code == 416
    assert attempts == ["bytes=0-0"]
    with pytest.raises(ValueError, match="unsupported HTTP header"):
        with local_raster_provider.safe_raster_source(
            "https://bounded-range.example/plot.tif",
            {"GDAL_HTTP_HEADERS": "X-Api-Key: must-not-be-forwarded"},
        ):
            pytest.fail("unsupported provider header must be rejected")


def test_safe_raster_source_reserves_shared_session_budget_for_parallel_ranges(monkeypatch):
    monkeypatch.setattr(local_raster_provider, "MAX_REMOTE_RASTER_SESSION_BYTES", 4)
    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "parallel-ranges.example", ["93.184.215.14"]),
    )
    source_bytes = b"0123456789"
    fetched_ranges = []

    def get(_url, *, headers, max_bytes, **_kwargs):
        range_header = headers["Range"]
        if range_header != "bytes=0-0":
            fetched_ranges.append(range_header)
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        end = min(end, len(source_bytes) - 1)
        body = source_bytes[start:end + 1]
        assert len(body) <= max_bytes
        return SimpleNamespace(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(source_bytes)}",
                "ETag": '"stable-object"',
            },
            content=body,
        )

    monkeypatch.setattr(public_http.public_http, "get", get)
    with local_raster_provider.safe_raster_source("https://parallel-ranges.example/plot.tif") as local_url:
        def fetch(range_header):
            return public_http.requests.get(
                local_url, headers={"Range": range_header}, timeout=3
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(fetch, ("bytes=0-2", "bytes=3-5")))

    assert sorted(statuses) == [206, 413]
    assert len(fetched_ranges) == 1


def test_rasterio_reads_cog_through_loopback_range_proxy(monkeypatch):
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    expected = np.arange(16, dtype="uint8").reshape((4, 4))
    with MemoryFile() as memory_file:
        with memory_file.open(
            driver="GTiff",
            width=4,
            height=4,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=from_origin(100, 4, 0.01, 0.01),
        ) as raster:
            raster.write(expected, 1)
        raster_bytes = memory_file.read()

    monkeypatch.setattr(
        public_http.public_http,
        "validate_public_destination",
        lambda url: (url, "cog.example", ["93.184.215.14"]),
    )
    ranges = []

    def get(_url, *, headers, max_bytes, **_kwargs):
        range_header = headers["Range"]
        ranges.append(range_header)
        start, end = (int(value) for value in range_header.removeprefix("bytes=").split("-"))
        end = min(end, len(raster_bytes) - 1)
        body = raster_bytes[start:end + 1]
        assert len(body) <= max_bytes
        return SimpleNamespace(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(raster_bytes)}",
                "ETag": '"test-raster"',
            },
            content=body,
        )

    monkeypatch.setattr(public_http.public_http, "get", get)
    with local_raster_provider.safe_raster_source("https://cog.example/tiny.tif") as source_url:
        with rasterio.open(source_url) as source:
            assert np.array_equal(source.read(1), expected)
    assert ranges and ranges[0] == "bytes=0-0"
    assert any(value != "bytes=0-0" for value in ranges)

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


def test_remote_raster_download_requires_exact_host_for_redirects(monkeypatch, tmp_path):
    resolved = []
    monkeypatch.setattr(public_http, 'public_addresses', lambda host: resolved.append(host) or ['93.184.215.14'])
    public_http._CIRCUITS.pop("assets.example", None)

    class Pool:
        def __init__(self, _address, **kwargs):
            self.hostname = kwargs["server_hostname"]
        def urlopen(self, *_args, **_kwargs):
            return SimpleNamespace(
                status=302,
                headers={"Location": "https://cdn.assets.example/data.tif"},
                close=lambda: None,
            )
        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, "HTTPSConnectionPool", Pool)
    target = tmp_path / "raster.tif"
    with pytest.raises(ValueError, match="original host"):
        public_http.public_http.download_to_file(
            "https://assets.example/data.tif",
            target,
            max_bytes=16,
            allowed_redirect_hosts={"assets.example"},
            same_host_redirects=True,
        )
    assert resolved == ["assets.example"]
    assert not target.exists()


@pytest.mark.parametrize(
    "headers,body",
    [({"Content-Length": "9"}, b""), ({}, b"123456789")],
)
def test_public_https_file_download_caps_declared_and_streamed_bytes(monkeypatch, tmp_path, headers, body):
    monkeypatch.setattr(public_http, "public_addresses", lambda _host: ["93.184.215.14"])
    hostname = "bounded-download.example"
    public_http._CIRCUITS.pop(hostname, None)

    class Pool:
        def __init__(self, *_args, **_kwargs):
            pass
        def urlopen(self, *_args, **_kwargs):
            stream = io.BytesIO(body)
            raw = SimpleNamespace(read1=lambda size: stream.read(size))
            return SimpleNamespace(status=200, headers=headers, _fp=raw, close=lambda: None)
        def close(self):
            pass

    monkeypatch.setattr(public_http.urllib3, "HTTPSConnectionPool", Pool)
    target = tmp_path / "too-large.bin"
    with pytest.raises(ValueError, match="size limit"):
        public_http.public_http.download_to_file(
            f"https://{hostname}/asset",
            target,
            max_bytes=8,
            allowed_redirect_hosts={hostname},
        )
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
