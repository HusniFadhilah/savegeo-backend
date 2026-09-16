from __future__ import annotations

from app.services.provenance_service import _aoi_checksum, _safe_parameters


def test_provenance_parameters_redact_secrets_and_store_aoi_by_checksum():
    payload = {
        "token": "do-not-store",
        "api_key": "do-not-store",
        "aoi": {"type": "Point", "coordinates": [100.0, -3.0]},
        "scale": 10,
    }
    safe = _safe_parameters(payload)
    assert safe["token"] == "[REDACTED]"
    assert safe["api_key"] == "[REDACTED]"
    assert safe["aoi"] == "[AOI_EXCLUDED; checksum stored separately]"
    assert len(_aoi_checksum(payload)) == 64
