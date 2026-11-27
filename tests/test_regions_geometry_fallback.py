from app.api.routes import regions


def _polygon():
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [114.1, -8.5],
                [114.2, -8.5],
                [114.2, -8.6],
                [114.1, -8.6],
                [114.1, -8.5],
            ]
        ],
    }


def test_missing_village_geometry_uses_exact_pum_fallback(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "kdebps": "",
                            "kdepum": "35.10.05.2003",
                            "kdcpum": "35.10.05",
                            "namobj": "Tembokrejo",
                        },
                        "geometry": _polygon(),
                    }
                ],
            }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(regions.requests, "get", fake_get)

    result = regions._fallback_village_geometry("3510052003")

    assert result["features"][0]["properties"]["namobj"] == "Tembokrejo"
    assert result["meta"]["boundary_year"] == 2025
    assert calls[0][0] == regions._VILLAGE_BOUNDARY_FALLBACK_URL
    assert calls[0][1]["params"]["where"] == "kdebps='3510052003' OR kdepum='35.10.05.2003'"
    assert calls[0][1]["params"]["outSR"] == "4326"


def test_village_fallback_rejects_wrong_parent_or_invalid_polygon(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            invalid = {
                "type": "Polygon",
                "coordinates": [[[114.0, -8.0], [114.2, -8.2], [114.0, -8.2], [114.2, -8.0], [114.0, -8.0]]],
            }
            return {
                "features": [
                    {
                        "properties": {"kdepum": "35.10.05.2003", "kdcpum": "35.10.06"},
                        "geometry": _polygon(),
                    },
                    {
                        "properties": {"kdepum": "35.10.05.2003", "kdcpum": "35.10.05"},
                        "geometry": invalid,
                    },
                ],
            }

    monkeypatch.setattr(regions.requests, "get", lambda *args, **kwargs: Response())

    assert regions._fallback_village_geometry("3510052003") is None


def test_missing_city_geometry_uses_exact_big_pum_code(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "kdbbps": " ",
                            "kdpkab": "16.12",
                            "kdppum": "16",
                            "namobj": "Penukal Abab Lematang Ilir",
                            "metadata": "TASWIL5000020230907KABKOTA",
                        },
                        "geometry": _polygon(),
                    }
                ],
            }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(regions.requests, "get", fake_get)

    result = regions._fallback_city_geometry("1612")

    assert result["features"][0]["properties"]["kdpkab"] == "16.12"
    assert result["meta"]["boundary_year"] == 2023
    assert calls[0][0] == regions._CITY_BOUNDARY_FALLBACK_URL
    assert calls[0][1]["params"]["where"] == "kdbbps='1612' OR kdpkab='16.12'"
    assert calls[0][1]["params"]["maxAllowableOffset"] == "0.0001"


def test_primary_village_feature_collection_without_polygon_uses_fallback(monkeypatch):
    fallback = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": _polygon(), "properties": {}}],
    }
    calls = []
    monkeypatch.setattr(regions, "_fallback_village_geometry", lambda code: calls.append(code) or fallback)

    result = regions._normalize_or_fallback_geometry(
        {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {}}]},
        "village",
        "3510052003",
    )

    assert result is fallback
    assert calls == ["3510052003"]


def test_valid_primary_village_geometry_does_not_use_fallback(monkeypatch):
    def fail_if_called(code):
        raise AssertionError(f"fallback should not run for {code}")

    monkeypatch.setattr(regions, "_fallback_village_geometry", fail_if_called)
    primary = {"type": "Feature", "geometry": _polygon(), "properties": {"code": "3510052003"}}

    assert regions._normalize_or_fallback_geometry(primary, "village", "3510052003") is primary
