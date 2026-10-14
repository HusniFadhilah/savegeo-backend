from app.services import landcover_service


class FakeVisualImage:
    def __init__(self):
        self.clip_geometry = None

    def clip(self, geometry):
        self.clip_geometry = geometry
        return self


def test_landcover_result_tile_is_clipped_to_requested_aoi(monkeypatch):
    aoi = object()
    visual_image = FakeVisualImage()
    monkeypatch.setattr(
        landcover_service,
        "summarize_landcover_classes",
        lambda *_args, **_kwargs: ({"Trees": {"area": 1}}, 1.0, 10),
    )
    monkeypatch.setattr(
        landcover_service,
        "landcover_visual_image",
        lambda *_args: (visual_image, {"min": 0, "max": 0, "palette": ["397d49"]}),
    )
    monkeypatch.setattr(
        landcover_service,
        "get_tile_url",
        lambda *_args: {"tile_url": "https://tiles.example/{z}/{x}/{y}"},
    )

    result = landcover_service.summarize_landcover_image(
        "Dynamic_World",
        image=object(),
        aoi=aoi,
        lc_scale=10,
        metadata={"year": 2026, "dataset_name": "Dynamic World"},
    )

    assert result["tile_url"].startswith("https://tiles.example/")
    assert visual_image.clip_geometry is aoi
