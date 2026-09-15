import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes.disaster import router
from app.core.security import get_current_disaster_viewer
from app.db.session import get_db

from app.services import fire_multi_source_service as fire
from app.services.gee_common import AnalysisError
from app.services.disaster_service import _inclusive_end

AOI = {
    "geojson": {
        "type": "Polygon",
        "coordinates": [
            [[110, -2], [111, -2], [111, -1], [110, -1], [110, -2]],
            [[110.4, -1.6], [110.6, -1.6], [110.6, -1.4], [110.4, -1.4], [110.4, -1.6]],
        ],
    }
}


def point(source="bmkg", lon=110.2, lat=-1.2, time="2026-01-01T01:00:00Z"):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"source_id": source, "acquired_at": time, "frp_mw": 0},
    }


class FireMultiSourceTests(unittest.TestCase):
    def test_exclusive_ee_end(self):
        self.assertEqual(_inclusive_end("2026-08-31"), "2026-09-01")
        self.assertEqual(_inclusive_end("2026-12-31"), "2027-01-01")

    def test_period_and_inclusive_limit(self):
        self.assertEqual(
            fire._period({"start_date": "2026-01-01", "end_date": "2026-01-31"}),
            (date(2026, 1, 1), date(2026, 1, 31)),
        )
        for end in ("2026-02-01", "2025-12-31", "2099-01-01", "invalid"):
            with self.assertRaises(AnalysisError):
                fire._period({"start_date": "2026-01-01", "end_date": end})

    def test_firms_preserves_zero_and_time(self):
        features = fire.parse_firms_csv(
            "latitude,longitude,acq_date,acq_time,confidence,frp\n-1.2,110.2,2026-01-01,15,n,0\n",
            "firms_noaa20",
        )
        props = features[0]["properties"]
        self.assertEqual(props["acquired_at"], "2026-01-01T00:15:00Z")
        self.assertEqual(props["frp_mw"], 0)
        self.assertEqual(props["original"]["confidence"], "n")

    def test_clip_polygon_holes(self):
        self.assertEqual(len(fire.clip_points([point(), point(lon=110.5, lat=-1.5), point(lon=112)], AOI)), 1)

    def test_merge_only_cross_source_close_in_space_and_time(self):
        result = fire.merge_observations(
            [
                point(),
                point("firms_noaa20", lon=110.201),
                point("bmkg"),
                point("firms_noaa21", time="2026-01-01T03:00:00Z"),
                point("firms_modis", lon=110.9),
            ]
        )
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["properties"]["observation_count"], 2)
        self.assertEqual(result[0]["properties"]["frp_mw"], 0)
        self.assertEqual(len(result[0]["properties"]["observations"]), 2)

    def test_missing_key_does_not_request(self):
        with (
            patch.object(fire, "get_settings", return_value=SimpleNamespace(nasa_firms_map_key="")),
            patch.object(fire.requests, "get") as get,
        ):
            self.assertEqual(
                fire._fetch_firms("firms_modis", [], date(2026, 1, 1), date(2026, 1, 1))["status"],
                "needs_key",
            )
            get.assert_not_called()

    def test_partial_error_is_sanitized(self):
        with (
            patch.object(fire, "_fetch_bmkg", side_effect=RuntimeError("secret-url-key")),
            patch.object(
                fire,
                "_fetch_cdse",
                return_value={"id": "cdse", "status": "ok", "kind": "footprints", "features": []},
            ),
        ):
            result = fire.load_sources(
                {
                    "aoi": AOI,
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-02",
                    "sources": ["bmkg", "cdse"],
                }
            )
        self.assertEqual([s["status"] for s in result["sources"]], ["error", "ok"])
        self.assertNotIn("secret-url-key", json.dumps(result))

    def test_import_semicolon_and_reject_aggregates(self):
        result = fire.import_observations(
            {"format": "csv", "content": "bujur;lintang;nama\n110.2;-1.2;laporan\n"}
        )
        self.assertEqual(result["features"][0]["geometry"]["coordinates"], [110.2, -1.2])
        self.assertEqual(result["features"][0]["properties"]["verification"], "user_import_not_verified")
        with self.assertRaises(AnalysisError):
            fire.import_observations({"format": "csv", "content": "provinsi,jumlah\nKalbar,20\n"})

    def test_import_rejects_invalid_geojson(self):
        for payload in (
            [],
            {"type": "FeatureCollection", "features": [None]},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [181, 0]}},
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []}},
        ):
            with self.assertRaises(AnalysisError):
                fire.import_observations({"format": "geojson", "content": json.dumps(payload)})

    def test_burn_without_ee(self):
        self.assertEqual(
            fire._fetch_burn("mcd64a1", AOI, date(2026, 1, 1), date(2026, 1, 2), False)["status"],
            "unavailable",
        )

    def test_unpublished_burn_month_has_no_fallback(self):
        collection = MagicMock()
        collection.filterDate.return_value = collection
        collection.size.return_value.getInfo.return_value = 0
        with patch.object(fire.ee, "ImageCollection", return_value=collection):
            result = fire._fetch_burn("mcd64a1", AOI, date(2026, 1, 1), date(2026, 1, 2), True)
        self.assertEqual(result["status"], "no_data")
        collection.filterDate.assert_called_once_with("2026-01-01", "2026-02-01")
        collection.map.assert_not_called()

    def test_unknown_sources(self):
        for sources in (["unknown"], [], [None]):
            with self.assertRaises(AnalysisError):
                fire.load_sources(
                    {"aoi": AOI, "start_date": "2026-01-01", "end_date": "2026-01-02", "sources": sources}
                )


class FireRouteContractTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")
        self.app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(self.app)

    def test_read_endpoints_require_authentication(self):
        for endpoint in ("fire-multi-source", "fire-big-boundaries", "fire-import"):
            self.assertEqual(self.client.post(f"/api/disaster/{endpoint}", json={}).status_code, 401)

    def test_partial_source_response_does_not_require_ee(self):
        self.app.dependency_overrides[get_current_disaster_viewer] = lambda: object()
        with (
            patch.object(
                fire, "_fetch_bmkg", return_value={"id": "bmkg", "status": "ok", "features": [point()]}
            ),
            patch.object(fire, "get_settings", return_value=SimpleNamespace(nasa_firms_map_key="")),
        ):
            response = self.client.post(
                "/api/disaster/fire-multi-source",
                json={
                    "aoi": AOI,
                    "start_date": "2026-01-01",
                    "end_date": "2026-01-02",
                    "sources": ["firms_noaa20", "bmkg", "mcd64a1"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [source["status"] for source in response.json()["sources"]], ["needs_key", "ok", "unavailable"]
        )
        self.assertEqual(response.json()["hotspots"]["type"], "FeatureCollection")

    def test_invalid_request_returns_400(self):
        self.app.dependency_overrides[get_current_disaster_viewer] = lambda: object()
        for endpoint in ("fire-multi-source", "fire-big-boundaries", "fire-import"):
            self.assertEqual(self.client.post(f"/api/disaster/{endpoint}", json=[]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
