from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.api.routes import admin_disaster
from app.services import disaster_capability_service, wildfire_hotspot_service


def _event(**changes):
    values = {
        "id": 40,
        "name": "Karhutla Kalimantan 2026",
        "disaster_type": "forest_fire",
        "source": "NASA FIRMS / Kementerian Kehutanan",
        "slug": "kalimantan-2026",
        "start_date": date(2026, 8, 20),
        "monitoring_from": date(2026, 8, 20),
        "province": ["Kalimantan Barat", "Kalimantan Tengah"],
        "province_codes": ["61", "62"],
        "boundary_source": "Peta Administratif",
        "methodology": "Deteksi panas satelit untuk pemantauan kebakaran.",
        "last_data_at": datetime(2026, 10, 6, 13, 27, tzinfo=timezone.utc),
        "hotspot_last_synced_at": datetime(2026, 10, 6, 16, 11, tzinfo=timezone.utc),
        "status": "draft",
        "is_public": False,
    }
    values.update(changes)

    class Event(SimpleNamespace):
        def to_dict(self):
            return {"slug": self.slug, "status": self.status, "is_public": self.is_public}

    return Event(**values)


def test_wildfire_publication_uses_hotspot_and_boundary_readiness(monkeypatch):
    monkeypatch.setattr(wildfire_hotspot_service, "has_event_rows", lambda _db, _event_id: True)

    result = disaster_capability_service.event_publication_check(object(), _event())

    assert result.allowed is True
    assert result.status == "validated"
    assert result.reasons == ()


def test_wildfire_publication_is_blocked_without_published_hotspots(monkeypatch):
    monkeypatch.setattr(wildfire_hotspot_service, "has_event_rows", lambda _db, _event_id: False)

    result = disaster_capability_service.event_publication_check(object(), _event())

    assert result.allowed is False
    assert "Data hotspot terverifikasi belum tersedia" in result.reasons


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"province_codes": []}, "Cakupan provinsi dan kode batas wilayah belum lengkap"),
        ({"methodology": None}, "Metodologi pemantauan belum tersedia"),
        ({"slug": None}, "Slug kejadian karhutla belum tersedia"),
    ],
)
def test_wildfire_publication_requires_public_page_metadata(monkeypatch, changes, reason):
    monkeypatch.setattr(wildfire_hotspot_service, "has_event_rows", lambda _db, _event_id: True)

    result = disaster_capability_service.event_publication_check(object(), _event(**changes))

    assert result.allowed is False
    assert reason in result.reasons


def test_admin_can_publish_wildfire_event_once_hotspot_gate_passes(monkeypatch):
    event = _event()
    admin = SimpleNamespace(id=7)
    monkeypatch.setattr(admin_disaster, "_get_event_or_404", lambda _db, _event_id: event)
    monkeypatch.setattr(wildfire_hotspot_service, "has_event_rows", lambda _db, _event_id: True)

    def update_event(_db, current_event, changes):
        for field, value in changes.items():
            setattr(current_event, field, value)
        current_event.is_public = current_event.status == "published"
        return current_event

    monkeypatch.setattr(admin_disaster.disaster_repo, "update_event", update_event)
    monkeypatch.setattr(admin_disaster.audit_service, "log_audit", lambda *_args, **_kwargs: None)

    response = admin_disaster.admin_update_event(
        id=event.id,
        payload=admin_disaster.EventUpdateRequest(status="published"),
        admin=admin,
        _perm=admin,
        db=object(),
    )

    assert response["status"] == "published"
    assert response["is_public"] is True

