"""Contract generation checks; no HTTP requests or credentials are used."""
import json
from scripts.build_postman_collection import build_collection, _sample_value


def requests(collection):
    def walk(items):
        for item in items:
            if "request" in item:
                yield item
            yield from walk(item.get("item", []))
    return list(walk(collection["item"]))


def spec_for(path, method="post", operation=None, **path_options):
    return {"info": {"version": "test"}, "paths": {path: {**path_options, method: operation or {"responses": {"200": {}, "422": {}}}}}}


def test_headers_and_path_level_parameters_are_preserved():
    spec = spec_for("/api/dmrv/projects/{project_id}/analysis-runs", operation={
        "responses": {"202": {}}, "parameters": [
            {"in": "header", "name": "Idempotency-Key", "required": True, "schema": {"type": "string"}},
            {"in": "header", "name": "If-Match", "required": False, "schema": {"type": "string"}},
        ]}, parameters=[{"in": "query", "name": "limit", "schema": {"type": "integer", "default": 20}}])
    item = requests(build_collection(spec))[-1]
    headers = {h["key"]: h for h in item["request"]["header"]}
    assert headers["Idempotency-Key"]["value"] == "{{requestIdempotencyKey}}"
    assert headers["If-Match"]["value"] == '\"{{revision}}\"'
    assert not headers["If-Match"].get("disabled")
    assert item["request"]["url"]["query"][0]["value"] == "20"


def test_no_body_is_fabricated_for_bodyless_actions():
    item = requests(build_collection(spec_for("/api/admin/gee/credentials/{cred_id}/activate")))[-1]
    assert "body" not in item["request"]


def test_array_samples_obey_minimum_size():
    assert _sample_value({"type": "array", "minItems": 2, "items": {"type": "integer", "minimum": 3}}, "items", {}) == [3, 3]


def test_mfa_template_and_durable_job_identity():
    operation = {"responses": {"200": {}}, "requestBody": {"content": {"application/json": {"schema": {
        "type": "object", "required": ["challenge", "code"], "properties": {"challenge": {"type": "string"}, "code": {"type": "string"}}
    }}}}}
    item = requests(build_collection(spec_for("/api/admin/auth/mfa/verify", operation=operation)))[-1]
    assert json.loads(item["request"]["body"]["raw"]) == {"challenge": "{{mfaChallenge}}", "code": "{{mfaCode}}"}
    item = requests(build_collection(spec_for("/api/dmrv/projects/{project_id}/jobs/{job_id}", method="get")))[-1]
    assert "{{dmrvJobId}}" in item["request"]["url"]["raw"]


def test_normal_mode_requires_success_and_5xx_is_never_allowed():
    item = requests(build_collection(spec_for("/api/test")))[-1]
    script = "\n".join(item["event"][0]["script"]["exec"])
    assert "successStatuses = [200]" in script
    assert "allowExpectedErrors" in script
    assert "to.be.below(500)" in script
    assert "503" not in script
