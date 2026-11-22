"""Probe one credential without changing Earth Engine's process-global client."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass

import requests
from google.auth.exceptions import GoogleAuthError, TransportError
from google.oauth2 import service_account

from app.core.config import get_settings
from app.core.pinned_provider_transport import pinned_google_authorized_session


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    code: str
    checked_at: str
    project_id: str | None = None
    client_email: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def probe(raw: bytes) -> ProbeResult:
    checked = dt.datetime.now(dt.UTC).isoformat()
    project, email = None, None
    try:
        data = json.loads(raw)
        # A credential's own Cloud project is the safest default. A global
        # override can point at a different project and make a valid key fail
        # authentication or EE initialization on production.
        project = data.get("project_id") or get_settings().gee_project_id
        email = data.get("client_email")
        if (data.get("type") != "service_account" or not isinstance(project, str)
                or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{3,127}", project)
                or data.get("token_uri") not in {"https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"}):
            return ProbeResult(False, "invalid_key_metadata", checked)
        credentials = service_account.Credentials.from_service_account_info(
            data, scopes=["https://www.googleapis.com/auth/earthengine"])
        with pinned_google_authorized_session(credentials, refresh_timeout=15) as session:
            response = session.post(f"https://earthengine.googleapis.com/v1/projects/{project}/value:compute",
                                    json={"expression": {
                                        "values": {"probe_constant": {"constantValue": 1}},
                                        "result": "probe_constant",
                                    }}, timeout=(10, 20))
        if response.status_code == 200 and response.json().get("result") == 1:
            return ProbeResult(True, "ok", checked, project, email)
        code = {401: "authentication_failed", 403: "permission_denied", 429: "rate_limited"}.get(response.status_code, "provider_error")
        return ProbeResult(False, code, checked, project, email)
    except (ValueError, TypeError, AttributeError, KeyError):
        return ProbeResult(False, "invalid_key", checked, project, email)
    except TransportError:
        # TransportError is also a GoogleAuthError, but it means the token
        # endpoint could not be reached. Do not misreport a network outage as
        # an invalid or revoked service-account key.
        return ProbeResult(False, "connection_failed", checked, project, email)
    except GoogleAuthError:
        return ProbeResult(False, "authentication_failed", checked, project, email)
    except requests.RequestException:
        return ProbeResult(False, "connection_failed", checked, project, email)
