from types import SimpleNamespace

from app.inference import carbon_inference


class _FakeImage:
    def __init__(self, bands):
        self.bands = list(bands)

    def select(self, names):
        return _FakeImage([names] if isinstance(names, str) else names)

    def rename(self, names):
        return _FakeImage([names] if isinstance(names, str) else names)

    def addBands(self, other):
        return _FakeImage(self.bands + other.bands)

    def multiply(self, _other):
        return _FakeImage(self.bands)

    def eq(self, _value):
        return _FakeImage(self.bands)


def test_dem_landcover_stack_includes_stac_training_interactions(monkeypatch):
    fake_ee = SimpleNamespace(
        Image=lambda _asset: _FakeImage(["elevation"]),
        ImageCollection=lambda _asset: SimpleNamespace(
            first=lambda: _FakeImage(["Map"])
        ),
        Terrain=SimpleNamespace(
            slope=lambda _dem: _FakeImage(["slope"]),
            aspect=lambda _dem: _FakeImage(["aspect"]),
        ),
    )
    monkeypatch.setattr(carbon_inference, "ee", fake_ee)

    engine = carbon_inference.CarbonInferenceEngine.__new__(
        carbon_inference.CarbonInferenceEngine
    )
    engine._safe_s2_composite = lambda *_args: _FakeImage(carbon_inference.S2_BANDS)
    engine._add_s2_indices = lambda *_args: _FakeImage(carbon_inference.S2_INDEX_BANDS)

    stack = engine._build_s2_dem_landcover_feature_stack(
        roi=object(), year=2025, start_month=1, end_month=12, cloud_threshold=50
    )

    assert {
        "NDVI_x_elevation",
        "NDMI_x_slope",
        "forest_mask_x_NDVI",
        "B8_x_B11",
    }.issubset(stack.bands)

