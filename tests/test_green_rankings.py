import json
from types import SimpleNamespace

import pytest

from app.services import green_rankings as service
from app.services.gee_common import AnalysisError


def test_hierarchy_supports_national_villages_and_parent_filters_with_resumable_cursor(monkeypatch):
    catalog = {
        (0, None): [("A", "1"), ("B", "2")],
        (1, "1"): [("A city", "11")],
        (1, "2"): [("B city", "21")],
        (2, "11"): [("A district", "111")],
        (2, "21"): [("B district", "211")],
        (3, "111"): [("A village", "1111"), ("A2", "1112")],
        (3, "211"): [("B village", "2111")],
    }
    monkeypatch.setattr(service, "dropdown", lambda index, parent=None: catalog[(index, parent)])
    stack = [{"level": 0, "items": catalog[(0, None)], "index": 0, "parents": {}}]
    first = service.next_region(stack, {"level": "village"})
    assert first["province"] == "1" and first["district"] == "111"
    stack = json.loads(json.dumps(stack))
    assert service.next_region(stack, {"level": "village"})["code"] == "1112"
    assert service.next_region(stack, {"level": "village"})["code"] == "2111"
    assert service.next_region(stack, {"level": "village"}) is None
    stack = [{"level": 0, "items": catalog[(0, None)], "index": 0, "parents": {}}]
    assert service.next_region(stack, {"level": "district", "province": "2"})["code"] == "211"
    assert service.next_region(stack, {"level": "district", "province": "2"}) is None


def test_rankings_use_percent_not_absolute_area_and_keep_partial_and_missing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(upload_path=tmp_path))
    with service.connection() as db:
        db.execute(
            "INSERT INTO runs(id,owner,config,stack) VALUES(?,?,?,?)",
            (
                "run",
                "admin:1",
                json.dumps(
                    {"level": "village", "year": 2026, "start_month": 1, "end_month": 12, "scale": 10}
                ),
                "[]",
            ),
        )
        for row in [
            dict(code="big", name="Big", eligible=True, area_ha=1000, green_area_ha=500, green_percent=50),
            dict(code="small", name="Small", eligible=True, area_ha=100, green_area_ha=90, green_percent=90),
            dict(code="missing", name="=unsafe CSV label", eligible=False, error="No boundary"),
        ]:
            service._insert(db, "run", row)
        db.commit()
    result = service.result_page("run", "admin:1")
    assert result["status"] == "partial"
    assert result["rows"][0]["code"] == "small"
    assert result["rows"][-1]["rank"] is None
    assert [row["rank"] for row in result["rows"]] == [1, 2, None]
    descending = service.result_page("run", "admin:1", sort_by="rank", sort_dir="desc")
    assert [row["code"] for row in descending["rows"]] == ["big", "small", "missing"]
    assert [row["rank"] for row in descending["rows"]] == [2, 1, None]
    by_percent = service.result_page("run", "admin:1", sort_by="green_percent", sort_dir="desc")
    assert [row["code"] for row in by_percent["rows"]] == ["small", "big", "missing"]
    injected_sort = service.result_page("run", "admin:1", sort_by="rank DESC; DROP TABLE rows")
    assert [row["code"] for row in injected_sort["rows"]] == ["small", "big", "missing"]
    assert service.result_page("run", "admin:1")["statistics"]["processed"] == 3
    assert result["statistics"]["processed"] == 3
    assert result["statistics"]["eligible"] == 2
    assert result["statistics"]["median_percent"] == 70
    assert result["statistics"]["std_dev_percent"] == 20
    assert result["statistics"]["failed_geometry"] == 1
    assert result["statistics"]["area_weighted_green_percent"] == pytest.approx(590 / 1100 * 100)
    exported = "".join(service.csv_rows("run", "admin:1"))
    assert "'\u003dunsafe CSV label" in exported
    assert "missing" in exported
    with pytest.raises(AnalysisError):
        service.result_page("run", "user:2")


def test_new_run_returns_checkpoint_before_expensive_computation(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(upload_path=tmp_path))
    monkeypatch.setattr(service, "dropdown", lambda *args: [("Province", "11")])
    result = service.run_batch(
        {"_request_owner": "admin:1", "year": 2026, "extension_settings": {"level": "province"}}
    )
    assert result["run_id"] and result["cursor"]
    assert result["statistics"]["processed"] == 0
    assert result["done"] is False


def test_latest_snapshot_falls_back_to_nearest_saved_parent_scope(monkeypatch):
    config = {
        "level": "village",
        "year": 2026,
        "start_month": 1,
        "end_month": 12,
        "scale": 10,
        "algorithm": 1,
        "province": "11",
        "city": "1101",
        "district": "110101",
    }
    lookups = []

    def fake_scope_key(candidate):
        return "/".join(candidate.get(key, "-") for key in ("province", "city", "district"))

    def fake_snapshot_page(scope_key, **options):
        lookups.append((scope_key, options["filter_config"]))
        if scope_key == "11/1101/-":
            return {"run_id": "snapshot:nearest-parent", "config": config}
        raise AnalysisError("Snapshot tidak ditemukan.", 404)

    monkeypatch.setattr(service, "snapshot_scope_key", fake_scope_key)
    monkeypatch.setattr(service, "snapshot_page", fake_snapshot_page)

    result = service.latest_snapshot(config, page=2)

    assert result["run_id"] == "snapshot:nearest-parent"
    assert [scope for scope, _ in lookups] == ["11/1101/110101", "11/1101/-"]
    assert all(requested is config for _, requested in lookups)
