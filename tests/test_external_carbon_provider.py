from __future__ import annotations

from app.providers import external_carbon_provider as provider


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        depths = ["0-5cm", "5-15cm", "15-30cm"]

        def layer(name, values):
            return {
                "name": name,
                "depths": [{"label": label, "values": {"mean": value}} for label, value in zip(depths, values)],
            }

        return {
            "properties": {
                "layers": [
                    layer("soc", [100, 100, 100]),
                    layer("bdod", [100, 100, 100]),
                    layer("cfvo", [0, 100, 200]),
                ]
            }
        }


def test_soilgrids_stock_applies_coarse_fragment_correction(monkeypatch):
    monkeypatch.setattr(provider.public_http, "get", lambda *args, **kwargs: _Response())
    soc, stock, rock_fraction = provider._soilgrids_components_point_detailed(
        "https://soilgrids.test", 110.0, -7.0, [("0-5cm", 5), ("5-15cm", 10), ("15-30cm", 15)]
    )

    assert soc == 10.0
    # Uncorrected stock is 30 Mg C/ha; cfvo factors are 1.0, .9 and .8.
    assert stock == 26.0
    assert rock_fraction == (0.0 + 0.1 + 0.2) / 3


def test_soilgrids_rate_limit_is_not_retried_inside_provider(monkeypatch):
    calls = []

    class RateLimited:
        status_code = 429

        def raise_for_status(self):
            raise provider.requests.HTTPError("rate limited")

    monkeypatch.setattr(provider.public_http, "get", lambda *_args, **_kwargs: (calls.append(1), RateLimited())[1])
    monkeypatch.setattr(provider.time, "sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("provider must not add a second retry loop")))

    result = provider._soilgrids_point("https://soilgrids.test", 110, -7, [("0-5cm", 5)], 5)

    assert result is None
    assert len(calls) == 1
