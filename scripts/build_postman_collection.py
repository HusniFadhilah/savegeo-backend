"""Build an importable Postman collection and environments from FastAPI OpenAPI.

Run from ``backend``:
    python scripts/build_postman_collection.py

For deterministic review against a previously exported OpenAPI file:
    python scripts/build_postman_collection.py --openapi-path ../.tmp-openapi-postman-source.json
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
EXPECTED_APP_ERRORS = [400, 401, 403, 404, 409, 413, 415, 422, 429, 503]
SAMPLE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[110.39, -7.02], [110.40, -7.02], [110.40, -7.01], [110.39, -7.01], [110.39, -7.02]]],
}
PARAMETER_DEFAULTS = {
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
        return []
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


def _postman_script(expected: list[int]) -> list[str]:
    allowed = sorted(set(expected + EXPECTED_APP_ERRORS))
    return [
        f"const expectedStatuses = {json.dumps(allowed)};",
        "pm.test('HTTP status is expected (see endpoint description for auth/validation cases)', function () {",
        "    pm.expect(expectedStatuses, 'Unexpected HTTP ' + pm.response.code + ': ' + pm.response.text()).to.include(pm.response.code);",
        "});",
        "pm.test('Response time is under 120 seconds', function () {",
        "    pm.expect(pm.response.responseTime).to.be.below(120000);",
        "});",
        "try {",
        "    const data = pm.response.json();",
        "    if (data && data.job_id) pm.collectionVariables.set('jobId', String(data.job_id));",
        "    if (data && data.status_url) pm.collectionVariables.set('jobStatusUrl', String(data.status_url));",
        "} catch (e) { /* binary/empty response */ }",
    ]


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
        if method in {"POST", "PUT", "PATCH"} and path not in {
            "/api/admin/auth/logout", "/api/auth/logout", "/api/carbon/datasets/health/refresh"
        }:
            template = {
                "TODO": "OpenAPI tidak mencantumkan schema body untuk endpoint ini. Ganti dengan payload dari dokumentasi atau Network browser."
            }
            if path == "/api/analysis-jobs":
                template = {"type": "carbon", "payload": {}}
            return {
                "mode": "raw",
                "raw": json.dumps(template, ensure_ascii=False, indent=2),
                "options": {"raw": {"language": "json"}},
            }, ["Body schema tidak tersedia di OpenAPI. Template `TODO` sengaja ditandai agar tidak dianggap payload valid."]
        return None, []

    content = body.get("content", {})
    if "application/json" in content:
        schema = content["application/json"].get("schema", {})
        example = _sample_value(schema, "body", schemas)
        if path == "/api/analysis-jobs":
            example = {"type": "carbon", "payload": {}}
        if path in {"/api/auth/login", "/api/admin/auth/login"}:
            example = {"username": "{{username}}", "password": "{{password}}"}
            if path == "/api/admin/auth/login":
                example = {"username": "{{adminUsername}}", "password": "{{adminPassword}}"}
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
                item["value"] = str(value if value is not None else "")
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
                {"path": path, "method": method_upper, "operation": operation, "tag": tag}
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
                parameters = list(operation.get("parameters", []))
                path_with_vars = re.sub(r"\{([^{}]+)\}", r"{{\1}}", path)
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
                    "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": _postman_script([int(s) for s in operation.get("responses", {}) if str(s).isdigit()])}}],
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
        "description": "Endpoint dokumentasi FastAPI; terpisah dari 284 operasi API yang dihasilkan dari OpenAPI.",
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
            "version": "1.0.0",
        },
        "auth": {"type": "noauth"},
        "event": [{"listen": "prerequest", "script": {"type": "text/javascript", "exec": ["// Auth memakai session cookie setelah request login. Tidak ada credential yang ditanam di collection."]}}],
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
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    operations = sum(len(tag["item"][0].get("item", [])) + (len(tag["item"][1].get("item", [])) if len(tag["item"]) > 1 else 0) for tag in collection["item"])
    print(f"generated {operations} Postman requests in {len(collection['item'])} tag groups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
