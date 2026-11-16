"""Build an importable Postman collection and environments from FastAPI OpenAPI.

Run from ``backend``:
    python scripts/build_postman_collection.py

For deterministic review against a previously exported OpenAPI file:
    python scripts/build_postman_collection.py --openapi-path ../.tmp-openapi-postman-source.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
EXPECTED_APP_ERRORS = [400, 401, 403, 404, 409, 413, 415, 422, 429]
SAMPLE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[110.39, -7.02], [110.40, -7.02], [110.40, -7.01], [110.39, -7.01], [110.39, -7.02]]],
}
PARAMETER_DEFAULTS = {
    "project_id": "replace-with-project-uuid",
    "organization_id": "replace-with-organization-uuid",
    "period_id": "replace-with-period-uuid",
    "monitoring_period_id": "{{period_id}}",
    "baseline_period_id": "replace-with-baseline-period-uuid",
    "snapshot_id": "replace-with-snapshot-uuid",
    "boundary_id": "replace-with-boundary-uuid",
    "record_id": "replace-with-record-uuid",
    "document_id": "replace-with-document-uuid",
    "member_id": "replace-with-member-uuid",
    "invitation_id": "replace-with-invitation-uuid",
    "submission_id": "replace-with-submission-uuid",
    "package_id": "replace-with-package-uuid",
    "finding_id": "replace-with-finding-uuid",
    "challenge": "{{mfaChallenge}}",
    "action_id": "replace-with-action-id",
    "output_id": "result",
    "model_id": "1",
    "model_name": "ibm_granite_geospatial_biomass",
    "code": "11",
    "island_name": "Jawa",
    "iso3": "IDN",
    "level": "1",
    "cid": "1",
    "session_id": "replace-with-session-id",
    "cred_id": "1",
    "category": "carbon",
    "user_id": "1",
    "job_id": "replace-with-job-id",
    "key": "dynamic_world_v1",
    "dataset_key": "gedi_l4a_monthly",  # gitleaks:allow — OpenAPI sample value, not a credential.
    "z": "0",
    "x": "0",
    "y": "0",
    "dataset_id": "replace-with-dataset-id",
    "run_id": "replace-with-run-id",
    "dataset": "replace-with-dataset",
    "slug": "replace-with-slug",
    "layer_id": "replace-with-layer-id",
    "frame_id": "replace-with-frame-id",
    "event_id": "replace-with-event-id",
    "id": "1",
    "imagery_id": "1",
    "hotspot_id": "replace-with-hotspot-id",
    "field_id": "1",
    "filename": "sample.tif",
    "provider_key": "mapbox",
    "scene_id": "replace-with-scene-id",
    "workflow_id": "replace-with-workflow-id",
    "username": "{{username}}",
    "password": "{{password}}",
    "email": "{{email}}",
    "query": "replace-with-search-query",
    "year": "2026",
    "date": "2026-01-01",
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
}


def _resolve_ref(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/components/schemas/"):
        return schema
    name = ref.rsplit("/", 1)[-1]
    resolved = schemas.get(name, {})
    # Keep the original reference in descriptions, but resolve examples and fields.
    return resolved if isinstance(resolved, dict) else {}


def _normalize_schema(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    schema = _resolve_ref(schema, schemas)
    if "anyOf" in schema:
        candidates = [x for x in schema["anyOf"] if x.get("type") != "null"]
        return _normalize_schema(candidates[0], schemas) if candidates else {"type": "null"}
    if "oneOf" in schema:
        return _normalize_schema(schema["oneOf"][0], schemas)
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            normalized = _normalize_schema(part, schemas)
            merged["properties"].update(normalized.get("properties", {}))
            merged["required"].extend(normalized.get("required", []))
        merged["required"] = sorted(set(merged["required"]))
        return merged
    return schema


def _sample_value(schema: dict[str, Any], field: str, schemas: dict[str, Any], depth: int = 0) -> Any:
    if depth > 8:
        return None
    schema = _normalize_schema(schema, schemas)
    if "default" in schema:
        return schema["default"]
    if "example" in schema:
        return schema["example"]
    if schema.get("examples"):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    if field.casefold() in {"geojson", "geometry", "aoi", "aoi_geojson", "feature"}:
        return SAMPLE_POLYGON

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        result: dict[str, Any] = {}
        for name, child in props.items():
            if name in required or "default" in child or "example" in child or "enum" in child:
                result[name] = _sample_value(child, name, schemas, depth + 1)
        if result:
            return result
        if schema.get("additionalProperties") is True:
            return {}
        return {}
    if kind == "array":
        count = max(0, int(schema.get("minItems", 0)))
        return [_sample_value(schema.get("items", {}), field.rstrip("s"), schemas, depth + 1) for _ in range(count)]
    if kind == "integer":
        return int(schema.get("minimum", 1))
    if kind == "number":
        return float(schema.get("minimum", 0.5))
    if kind == "boolean":
        return False
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "binary":
            return ""
        if field.casefold() in {"username", "email", "password"}:
            return "{{" + field.casefold() + "}}"
        if field.casefold() in PARAMETER_DEFAULTS:
            return PARAMETER_DEFAULTS[field.casefold()]
        return "replace-me"
    return None


def _resolve_content_schema(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    return _normalize_schema(schema, schemas)


def _body_description(schema: dict[str, Any], schemas: dict[str, Any]) -> list[str]:
    schema = _resolve_content_schema(schema, schemas)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return ["OpenAPI tidak memublikasikan field body untuk operasi ini; isi payload sesuai kontrak endpoint."]
    lines = ["Field body:"]
    for name, prop in props.items():
        resolved = _normalize_schema(prop, schemas)
        type_name = resolved.get("type", "object" if "properties" in resolved else "value")
        enum = f"; pilihan: {', '.join(map(str, resolved['enum']))}" if resolved.get("enum") else ""
        lines.append(f"- `{name}` ({type_name}{', wajib' if name in required else ', opsional'}{enum})")
    return lines


def _parameter_sample(parameter: dict[str, Any], schemas: dict[str, Any]) -> str:
    schema = parameter.get("schema", {})
    default = parameter.get("example", schema.get("default"))
    if default is None:
        default = _sample_value(schema, parameter["name"], schemas)
    if isinstance(default, (dict, list)):
        return json.dumps(default, ensure_ascii=False, separators=(",", ":"))
    return str(default if default is not None else "replace-me")


def _query_is_enabled(parameter: dict[str, Any]) -> bool:
    return bool(parameter.get("required") or parameter.get("schema", {}).get("default") is not None)


def _postman_script(expected: list[int], path: str = "", method: str = "GET") -> list[str]:
    success = sorted({status for status in expected if 200 <= status < 300}) or [200, 201, 202, 204, 206]
    lines = [
        f"const successStatuses = {json.dumps(success)};",
        f"const negativeStatuses = {json.dumps(EXPECTED_APP_ERRORS)};",
        "const negativeMode = String(pm.variables.get('allowExpectedErrors')).toLowerCase() === 'true';",
        "pm.test('No server error or method mismatch', function () {",
        "    pm.expect(pm.response.code).to.be.below(500);",
        "    pm.expect(pm.response.code).not.to.equal(405);",
        "});",
        "pm.test('HTTP response matches the selected test mode', function () {",
        "    const allowed = negativeMode ? successStatuses.concat(negativeStatuses) : successStatuses;",
        "    pm.expect(allowed, 'HTTP ' + pm.response.code + ': ' + pm.response.text().slice(0, 1200)).to.include(pm.response.code);",
        "});",
        "pm.test('Response time is under 120 seconds', function () {",
        "    pm.expect(pm.response.responseTime).to.be.below(120000);",
        "});",
        "if (pm.response.code >= 200 && pm.response.code < 300) {",
        "  try {",
        "    const data = pm.response.json();",
    ]
    captures = {
        ("POST", "/api/organizations"): {"organization_id": "data.id"},
        ("POST", "/api/organizations/invitations/accept"): {"organization_id": "data.id"},
        ("POST", "/api/dmrv/projects"): {"project_id": "data.id"},
        ("POST", "/api/dmrv/projects/{project_id}/monitoring-periods"): {"period_id": "data.id"},
        ("POST", "/api/dmrv/projects/{project_id}/analysis-runs"): {"dmrvJobId": "data.id"},
        ("POST", "/api/dmrv/projects/{project_id}/jobs"): {"dmrvJobId": "data.id"},
        ("POST", "/api/dmrv/projects/{project_id}/evidence-packages"): {"package_id": "data.id"},
        ("POST", "/api/dmrv/projects/{project_id}/submissions"): {"submission_id": "data.id"},
    }.get((method, path), {})
    for variable, expression in captures.items():
        lines.append(f"    if ({expression}) pm.environment.set({json.dumps(variable)}, String({expression}));")
    lines += [
        "    if (data && data.job_id) { pm.environment.set('jobId', String(data.job_id)); pm.environment.set('job_id', String(data.job_id)); }",
        "    if (data && data.status_url) pm.environment.set('jobStatusUrl', String(data.status_url));",
        "    if (data && data.result && data.result.snapshot_id) pm.environment.set('snapshot_id', String(data.result.snapshot_id));",
        "    if (data && Number.isInteger(data.revision)) pm.environment.set('revision', String(data.revision));",
    ]
    if path == "/api/admin/auth/login":
        lines += [
            "    if (data && data.mfa_required && data.challenge) pm.environment.set('mfaChallenge', data.challenge);",
            "    else pm.environment.unset('mfaChallenge');",
        ]
    if path == "/api/admin/auth/mfa/verify":
        lines += ["    pm.environment.unset('mfaChallenge');", "    pm.environment.unset('mfaCode');"]
    lines += ["  } catch (e) { /* binary or empty response */ }", "}"]
    return lines


def _parameter_doc(parameters: list[dict[str, Any]], location: str, schemas: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    selected = [p for p in parameters if p.get("in") == location]
    if selected:
        lines.append(f"{location.title()} parameters:")
        for p in selected:
            required = "required" if p.get("required") else "optional"
            description = (p.get("description") or "").strip().replace("\n", " ")
            suffix = f" — {description}" if description else ""
            lines.append(f"- `{p['name']}` ({required}){suffix}")
    return lines


def _request_body(operation: dict[str, Any], path: str, method: str, schemas: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    body = operation.get("requestBody")
    if not body:
        return None, []

    content = body.get("content", {})
    if "application/json" in content:
        schema = content["application/json"].get("schema", {})
        example = _sample_value(schema, "body", schemas)
        if path == "/api/analysis-jobs":
            example = {"type": "carbon", "payload": {
                "aoi": {"geojson": SAMPLE_POLYGON}, "reference_dataset": "WCMC",
                "year": "{{analysisYear}}", "start_month": 1, "end_month": 12,
                "cloud_threshold": 20, "clip_to_aoi": True,
            }}
        if path in {"/api/auth/login", "/api/admin/auth/login"}:
            example = {"username": "{{username}}", "password": "{{password}}"}
            if path == "/api/admin/auth/login":
                example = {"username": "{{adminUsername}}", "password": "{{adminPassword}}"}
        if path.startswith("/api/admin/auth/mfa/") and isinstance(example, dict):
            if "challenge" in example:
                example["challenge"] = "{{mfaChallenge}}"
            if "code" in example:
                example["code"] = "{{mfaCode}}"
            if "password" in example:
                example["password"] = "{{adminPassword}}"
        if path == "/api/dmrv/projects/{project_id}/analysis-runs":
            example = {"kind": "carbon", "monitoring_period_id": "{{period_id}}", "parameters": {"reference_dataset": "WCMC"}}
        if example is None:
            example = {}
        schema_lines = _body_description(schema, schemas)
        if not schema_lines or not schema.get("properties") and "$ref" not in schema:
            schema_lines = ["Body JSON; cek kontrak route bila API mengembalikan validation error."]
        return {
            "mode": "raw",
            "raw": json.dumps(example, ensure_ascii=False, indent=2),
            "options": {"raw": {"language": "json"}},
        }, schema_lines

    if "multipart/form-data" in content:
        schema = _resolve_content_schema(content["multipart/form-data"].get("schema", {}), schemas)
        required = set(schema.get("required", []))
        fields = []
        for name, prop in schema.get("properties", {}).items():
            resolved = _normalize_schema(prop, schemas)
            is_file = resolved.get("format") == "binary" or name.casefold() in {"file", "files", "upload"}
            item: dict[str, Any] = {
                "key": name,
                "type": "file" if is_file else "text",
                "description": "required" if name in required else "optional",
            }
            if is_file:
                item["src"] = ""
            else:
                value = _sample_value(prop, name, schemas)
                item["value"] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else str(value if value is not None else "")
                if name not in required:
                    item["disabled"] = True
            fields.append(item)
        return {"mode": "formdata", "formdata": fields}, ["Pilih file lokal pada field bertipe file sebelum mengirim."]

    first_type = next(iter(content), "application/json")
    return {
        "mode": "raw",
        "raw": "{}",
        "options": {"raw": {"language": "json"}},
    }, [f"Content type `{first_type}`; template body perlu disesuaikan."]


def build_collection(spec: dict[str, Any]) -> dict[str, Any]:
    schemas = spec.get("components", {}).get("schemas", {})
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in METHODS or not isinstance(operation, dict):
                continue
            method_upper = method.upper()
            tags = operation.get("tags") or ["untagged"]
            tag = tags[0]
            category = "01 — Read-only (GET)" if method_upper in {"GET", "HEAD"} else "02 — Write / analysis (review first)"
            grouped.setdefault(tag, {"read": [], "write": []})["read" if category.startswith("01") else "write"].append(
                {"path": path, "method": method_upper, "operation": operation, "tag": tag, "path_parameters": path_item.get("parameters", [])}
            )

    collection_items: list[dict[str, Any]] = []
    all_tags = sorted(grouped, key=lambda s: (s not in {"user-auth", "health", "application-access"}, s.casefold()))
    for tag in all_tags:
        folders: list[dict[str, Any]] = []
        for group_key, folder_name in (("read", "01 — Read-only (GET)"), ("write", "02 — Write / analysis (review first)")):
            operations = sorted(
                grouped[tag][group_key],
                key=lambda x: (x["path"].casefold(), x["method"], x["operation"].get("summary", "").casefold()),
            )
            if not operations:
                continue
            requests = []
            for entry in operations:
                path = entry["path"]
                method = entry["method"]
                operation = entry["operation"]
                parameters = list(entry["path_parameters"]) + list(operation.get("parameters", []))
                path_with_vars = re.sub(r"\{([^{}]+)\}", r"{{\1}}", path)
                if path.startswith("/api/dmrv/"):
                    path_with_vars = path_with_vars.replace("{{job_id}}", "{{dmrvJobId}}")
                query_items = []
                raw_query: list[str] = []
                for p in [p for p in parameters if p.get("in") == "query"]:
                    value = _parameter_sample(p, schemas)
                    enabled = _query_is_enabled(p)
                    query_items.append({
                        "key": p["name"],
                        "value": value,
                        "description": p.get("description", ""),
                        **({"disabled": True} if not enabled else {}),
                    })
                    if enabled:
                        from urllib.parse import quote
                        raw_query.append(f"{quote(p['name'])}={quote(value, safe='{{}}:,[]')}")
                raw_url = "{{baseUrl}}" + path_with_vars
                if raw_query:
                    raw_url += "?" + "&".join(raw_query)
                url_obj: dict[str, Any] = {
                    "raw": raw_url,
                    "host": ["{{baseUrl}}"],
                    "path": [part for part in path_with_vars.strip("/").split("/") if part],
                }
                if query_items:
                    url_obj["query"] = query_items

                request_headers = [{"key": "Accept", "value": "application/json, application/problem+json, */*;q=0.8", "type": "text"}]
                for parameter in parameters:
                    if parameter.get("in") != "header":
                        continue
                    name = parameter["name"]
                    value = _parameter_sample(parameter, schemas)
                    if name.lower() == "idempotency-key":
                        value = "{{requestIdempotencyKey}}"
                    elif name.lower() == "if-match":
                        value = '\"{{revision}}\"'
                    request_headers.append({"key": name, "value": value, "type": "text",
                        "description": parameter.get("description", "Required" if parameter.get("required") else "Optional"),
                        **({"disabled": True} if not parameter.get("required") and name.lower() not in {"idempotency-key", "if-match"} else {})})
                body, body_notes = _request_body(operation, path, method, schemas)
                if body and body.get("mode") == "raw":
                    request_headers.append({"key": "Content-Type", "value": "application/json", "type": "text"})
                request: dict[str, Any] = {"method": method, "header": request_headers, "url": url_obj}
                if body:
                    request["body"] = body

                description_lines = [
                    f"### {operation.get('summary') or operation.get('operationId') or path}",
                    "",
                    f"- **Route:** `{method} {path}`",
                    f"- **Tag:** `{tag}`",
                    f"- **Operation ID:** `{operation.get('operationId', 'n/a')}`",
                    f"- **Auth:** {'dibutuhkan (cookie login atau Bearer token)' if operation.get('security') or spec.get('security') else 'tidak dinyatakan di OpenAPI'}",
                    f"- **Dampak:** {'read-only menurut metode HTTP; beberapa GET dapat tetap berat (tile, export, atau query eksternal).' if method in {'GET', 'HEAD'} else 'dapat mengubah data, mengirim berkas, menjalankan analisis, atau menghabiskan resource. Review dulu sebelum Send.'}",
                ]
                description_lines += ["", *_parameter_doc(parameters, "path", schemas)] if any(p.get("in") == "path" for p in parameters) else []
                description_lines += ["", *_parameter_doc(parameters, "query", schemas)] if any(p.get("in") == "query" for p in parameters) else []
                if body_notes:
                    description_lines += ["", "### Request body", *body_notes]
                if operation.get("description"):
                    description_lines += ["", "### Backend description", operation["description"]]
                description_lines += ["", "### Expected HTTP statuses", ", ".join(operation.get("responses", {}).keys()) or "response dinamis"]
                requests.append({
                    "name": f"{method} {path} — {operation.get('summary') or operation.get('operationId') or ''}".strip(),
                    "request": {**request, "description": "\n".join(description_lines)},
                    "response": [],
                    "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": _postman_script([int(s) for s in operation.get("responses", {}) if str(s).isdigit()], path, method)}}],
                })
            folders.append({
                "name": folder_name,
                "description": "Semua route dalam folder ini berasal dari kontrak OpenAPI backend.",
                "item": requests,
            })
        collection_items.append({
            "name": tag,
            "description": f"Grup route OpenAPI bertag `{tag}`. Semua operasi backend tercantum di bawah grup ini.",
            "item": folders,
        })

    docs_items = []
    for path, name, note in (
        ("/openapi.json", "OpenAPI JSON", "Spesifikasi OpenAPI yang dilayani FastAPI."),
        ("/docs", "Swagger UI", "Interactive API documentation."),
        ("/redoc", "ReDoc", "Alternative API documentation."),
        ("/docs/oauth2-redirect", "Swagger OAuth redirect", "Callback helper bawaan Swagger UI."),
    ):
        docs_items.append({
            "name": f"GET {path} — {name}",
            "request": {
                "method": "GET",
                "header": [{"key": "Accept", "value": "*/*", "type": "text"}],
                "url": {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": path.strip("/").split("/")},
                "description": f"### {name}\n\n- **Route:** `GET {path}`\n- {note}\n- Halaman dokumentasi tidak selalu tersedia di semua konfigurasi deployment.",
            },
            "response": [],
            "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": _postman_script([200])}}],
        })
    collection_items.insert(0, {
        "name": "00 — OpenAPI & documentation",
        "description": "Endpoint dokumentasi FastAPI; terpisah dari operasi API yang dihasilkan dari OpenAPI.",
        "item": docs_items,
    })

    variables = [
        ("baseUrl", "http://127.0.0.1:8000", "Origin backend tanpa suffix /api."),
        ("username", "", "Username akun app user untuk POST /api/auth/login."),
        ("password", "", "Password akun app user. Jangan commit credential."),
        ("adminUsername", "", "Username admin untuk POST /api/admin/auth/login."),
        ("adminPassword", "", "Password admin. Jangan commit credential."),
        ("email", "", "Email yang dipakai pada payload auth atau user."),
        ("accessToken", "", "Opsional: tempel JWT di header Authorization: Bearer secara manual bila dibutuhkan."),
        ("idempotencyKey", "", "Opsional: isi UUID tetap untuk retry/replay; kosong menghasilkan UUID baru per pengiriman."),
        ("revision", "1", "Revisi resource untuk If-Match. GET resource yang akan diubah terlebih dahulu."),
        ("mfaChallenge", "", "Challenge MFA dari login admin; sensitif, jangan dibagikan."),
        ("mfaCode", "", "TOTP 6 digit atau recovery code sekali pakai. Isi secara manual."),
        ("analysisYear", str(datetime.now(timezone.utc).year), "Tahun analisis; tahun berjalan dapat memiliki cakupan parsial."),
        ("dmrvJobId", "replace-with-dmrv-job-uuid", "ID job durable dMRV, terpisah dari job eksplorasi."),
        ("allowExpectedErrors", "false", "false: hanya 2xx sesuai kontrak lulus. true: aktifkan hanya untuk tes negatif."),
        ("jobId", "replace-with-job-id", "Job id hasil POST /api/analysis-jobs."),
        ("jobStatusUrl", "/api/analysis-jobs/replace-with-job-id", "status_url dari response analysis job."),
        ("aoiGeojson", json.dumps(SAMPLE_POLYGON, ensure_ascii=False), "Contoh AOI polygon di sekitar Semarang; ganti sesuai wilayah uji."),
    ]
    path_names = set(re.findall(r"\{([^{}]+)\}", " ".join(spec.get("paths", {}).keys())))
    for name in sorted(path_names):
        variables.append((name, PARAMETER_DEFAULTS.get(name.casefold(), "replace-me"), f"Nilai path parameter `{name}`; sesuaikan dengan record yang tersedia."))

    return {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": "SaveGeo Backend API — Complete",
            "description": (
                f"Collection dihasilkan dari OpenAPI SaveGeo, versi API {spec.get('info', {}).get('version', 'unknown')}. "
                f"Termasuk {sum(len(group['read']) + len(group['write']) for group in grouped.values())} operasi API dalam {len(grouped)} tag.\n\n"
                "Mulai dengan memilih environment, lalu kirim login user atau admin agar Postman menyimpan session cookie. "
                "Jangan gunakan Run Collection terhadap production: banyak POST/PUT/DELETE memodifikasi data atau memulai proses analisis. "
                "Template yang ditandai TODO perlu diisi dari kontrak route atau request browser Network."
            ),
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "version": "2.0.0",
        },
        "auth": {"type": "noauth"},
        "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": [
            "// Cookie jar supplies authentication. A fixed override allows deliberate idempotency retries.",
            "pm.variables.set('requestIdempotencyKey', pm.environment.get('idempotencyKey') || pm.variables.replaceIn('{{$guid}}'));",
        ]}}],
        "item": collection_items,
        "variable": [{"key": k, "value": v, "type": "string", "description": d} for k, v, d in variables],
    }


def _environment(name: str, base_url: str, description: str) -> dict[str, Any]:
    values = [
        {"key": "baseUrl", "value": base_url, "type": "default", "enabled": True},
        {"key": "username", "value": "", "type": "secret", "enabled": True},
        {"key": "password", "value": "", "type": "secret", "enabled": True},
        {"key": "adminUsername", "value": "", "type": "secret", "enabled": True},
        {"key": "adminPassword", "value": "", "type": "secret", "enabled": True},
        {"key": "email", "value": "", "type": "secret", "enabled": True},
        {"key": "accessToken", "value": "", "type": "secret", "enabled": True},
        {"key": "jobId", "value": "replace-with-job-id", "type": "default", "enabled": True},
        {"key": "jobStatusUrl", "value": "/api/analysis-jobs/replace-with-job-id", "type": "default", "enabled": True},
        {"key": "aoiGeojson", "value": json.dumps(SAMPLE_POLYGON, ensure_ascii=False), "type": "default", "enabled": True},
    ]
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "values": values,
        "_postman_variable_scope": "environment",
        "_postman_exported_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "_postman_exported_using": "SaveGeo Postman Collection Builder",
        "_postman_environment_description": description,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openapi-path", type=Path, help="Use an exported OpenAPI JSON file instead of importing app.main.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "postman")
    args = parser.parse_args()

    if args.openapi_path:
        spec = json.loads(args.openapi_path.read_text(encoding="utf-8"))
    else:
        from app.main import app

        spec = app.openapi()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    collection = build_collection(spec)
    previous_collection = args.output_dir / "SaveGeo-Backend.postman_collection.json"
    if previous_collection.exists():
        previous = json.loads(previous_collection.read_text(encoding="utf-8-sig"))
        previous_id = previous.get("info", {}).get("_postman_id")
        if previous_id:
            collection["info"]["_postman_id"] = previous_id
    targets = {
        "SaveGeo-Backend.postman_collection.json": collection,
        "SaveGeo-Local.postman_environment.json": _environment(
            "SaveGeo Local", "http://127.0.0.1:8000", "Backend FastAPI lokal langsung (default port 8000)."
        ),
        "SaveGeo-Production.postman_environment.json": _environment(
            "SaveGeo Production", "https://savegeo.len.co.id", "Production SaveGeo; jalankan hanya request yang aman dan disengaja."
        ),
    }
    for name, data in targets.items():
        path = args.output_dir / name
        if name.endswith("postman_environment.json"):
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(existing, dict) or not isinstance(existing.get("values"), list):
                    raise ValueError(f"Invalid environment structure: {path.name}; original file preserved")
                data["id"] = existing.get("id", data["id"])
                by_key = {entry["key"]: entry for entry in data["values"]}
                by_key.update({entry["key"]: entry for entry in existing["values"]})
                data["values"] = list(by_key.values())
            keys = {entry["key"] for entry in data["values"]}
            secret_keys = {"mfaChallenge", "mfaCode"}
            for variable in collection["variable"]:
                key = variable["key"]
                if key not in keys:
                    data["values"].append({"key": key, "value": variable["value"],
                        "type": "secret" if key in secret_keys else "default", "enabled": True})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.name}")
    spec_bytes = (json.dumps(spec, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (args.output_dir / "SaveGeo-OpenAPI.json").write_bytes(spec_bytes)
    operations = [(method.upper(), path, operation.get("tags", ["untagged"])[0])
        for path, item in spec.get("paths", {}).items() for method, operation in item.items()
        if method.lower() in METHODS and isinstance(operation, dict)]
    timestamp = datetime.now(timezone.utc).isoformat()
    lines = ["# Cakupan Collection SaveGeo", "", f"Dibuat (UTC): {timestamp}",
        f"Operasi API: **{len(operations)}**; grup tag: **{len({tag for _, _, tag in operations})}**.",
        "Ditambah empat endpoint dokumentasi FastAPI (ketersediaannya tergantung deployment).", "",
        f"SHA-256 OpenAPI: `{hashlib.sha256(spec_bytes).hexdigest()}`", "",
        "Sumber: kode backend lokal saat generator dijalankan; bukan bukti semua perubahan sudah terdeploy di production.",
        "Tidak ada request mutasi atau analisis dikirim saat pembuatan collection.", "",
        "| Metode | Endpoint | Grup |", "|---|---|---|"]
    lines.extend(f"| {method} | `{path}` | {tag} |" for method, path, tag in sorted(operations))
    (args.output_dir / "COVERAGE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"generated {len(operations)} API requests plus 4 documentation requests")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
