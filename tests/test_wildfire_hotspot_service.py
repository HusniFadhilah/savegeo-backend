import datetime as dt
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import wildfire_hotspot_service as wildfire
from app.services.wildfire_geometry import is_kalimantan_geometry


def _db_for_sync() -> MagicMock:
    db = MagicMock()
    existing_result = MagicMock()
    existing_result.scalars.return_value = []
    latest_result = MagicMock()
    latest_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [existing_result, latest_result]
    return db


def test_date_chunks_cover_period_without_gaps():
    chunks = list(wildfire._date_chunks(date(2026, 8, 1), date(2026, 9, 2)))

    assert chunks == [
        (date(2026, 8, 1), date(2026, 8, 5)),
        (date(2026, 8, 6), date(2026, 8, 10)),
        (date(2026, 8, 11), date(2026, 8, 15)),
        (date(2026, 8, 16), date(2026, 8, 20)),
        (date(2026, 8, 21), date(2026, 8, 25)),
        (date(2026, 8, 26), date(2026, 8, 30)),
        (date(2026, 8, 31), date(2026, 9, 2)),
    ]


def test_fetch_complete_period_splits_truncated_days_into_smaller_areas():
    requests = []

    def fake_fetch(source, from_date, to_date, bbox, **kwargs):
        requests.append((from_date, to_date, bbox))
        truncated = from_date != to_date or bbox[2] - bbox[0] > 6
        features = [] if truncated else [{"id": f"{from_date}:{bbox[0]}:{bbox[1]}", "properties": {}}]
        return {"features": features, "metadata": {"truncated": truncated}}

    with patch.object(wildfire.firms_service, "get_fires_for_period", side_effect=fake_fetch):
        features = wildfire._fetch_complete_period(
            "VIIRS_NOAA20_NRT",
            date(2026, 10, 1),
            date(2026, 10, 2),
            (108.8, -4.6, 119.4, 4.4),
        )

    assert len(features) == 8
    assert len(requests) == 11
    assert all(request[0] == request[1] for request in requests[1:])


def test_fetch_complete_period_fails_instead_of_accepting_truncated_leaf():
    with patch.object(
        wildfire.firms_service,
        "get_fires_for_period",
        return_value={"features": [], "metadata": {"truncated": True}},
    ):
        try:
            wildfire._fetch_complete_period(
                "VIIRS_NOAA20_NRT",
                date(2026, 10, 1),
                date(2026, 10, 1),
                (108.8, -4.6, 119.4, 4.4),
                spatial_depth=wildfire.MAX_SPATIAL_SPLIT_DEPTH,
            )
        except wildfire.IncompleteFirmsSyncError as exc:
            assert exc.status_code == 502
        else:
            raise AssertionError("Truncated leaf must abort sync")


def test_existing_row_lookup_chunks_large_id_lists():
    db = MagicMock()
    batches = []

    def fake_execute(statement):
        batches.append(statement.whereclause.clauses[1].right.value)
        result = MagicMock()
        result.scalars.return_value = []
        return result

    db.execute.side_effect = fake_execute
    with patch.object(wildfire, "EXISTING_ID_QUERY_BATCH_SIZE", 2):
        rows = wildfire._existing_rows_by_external_id(db, 7, [f"id-{index}" for index in range(5)])

    assert rows == {}
    assert [len(batch) for batch in batches] == [2, 2, 1]


def test_sync_fetches_each_firms_source_and_deduplicates_in_db():
    db = _db_for_sync()
    event = SimpleNamespace(
        id=7,
        bbox=[108.8, -4.6, 119.4, 4.4],
        monitoring_from=date(2026, 8, 20),
        monitoring_to=date(2026, 8, 27),
        start_date=None,
        end_date=None,
        hotspot_last_synced_at=None,
        last_synced_at=None,
        last_data_at=None,
    )

    def fake_fetch(source, **kwargs):
        return {
            "features": [
                {
                    "type": "Feature",
                    "id": f"firms-{source}",
                    "geometry": {"type": "Point", "coordinates": [110.2, -1.2]},
                    "properties": {
                        "source": source,
                        "instrument": "VIIRS",
                        "acq_datetime_utc": "2026-08-20T01:00:00Z",
                        "acq_date": "2026-08-20",
                        "confidence_label": "high",
                    },
                }
            ],
            "metadata": {"errors": []},
        }

    with patch.object(wildfire.firms_service, "get_fires_for_period", side_effect=fake_fetch) as fetch:
        result = wildfire.sync_event_hotspots(db, event)

    assert fetch.call_count == 2 * len(wildfire.firms_service.FIRMS_SOURCE_CATALOG)
    assert result["stored"] == len(wildfire.firms_service.FIRMS_SOURCE_CATALOG)
    assert result["from"] == "2026-08-20"
    assert result["to"] == "2026-08-27"
    assert db.add.call_count == result["stored"]
    assert event.hotspot_last_synced_at is not None


def test_active_event_period_defaults_to_today_instead_of_stale_last_data():
    event = SimpleNamespace(
        monitoring_from=date(2026, 8, 20),
        monitoring_to=None,
        start_date=date(2026, 1, 1),
        end_date=None,
        last_data_at=dt.datetime(2026, 8, 27, tzinfo=dt.UTC),
    )

    start, end = wildfire._event_period(event, None, None)

    assert start == date(2026, 8, 20)
    assert end == dt.datetime.now(dt.UTC).date()


def test_external_id_is_scoped_by_source():
    feature = {"id": "firms-abc", "properties": {"source": "VIIRS_NOAA20_NRT"}}

    assert wildfire._external_id(feature) == "VIIRS_NOAA20_NRT:firms-abc"


def test_kalimantan_mask_excludes_western_sulawesi():
    assert is_kalimantan_geometry({"type": "Point", "coordinates": [113.0, -1.0]})
    assert not is_kalimantan_geometry({"type": "Point", "coordinates": [119.0, -1.0]})
