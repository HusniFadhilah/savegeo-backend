import io
import unittest
from unittest.mock import patch

import httpx
import numpy as np
from PIL import Image

from app.services import imagery_service as service
from app.services.gee_common import AnalysisError
from app.services import samgeo_service


class StacTests(unittest.TestCase):
    def test_get_fallback_pagination_and_cloud_filter(self):
        calls = []

        def feature(name, cloud):
            return {"type": "Feature", "id": name, "bbox": [0, 0, 1, 1],
                    "properties": {"datetime": "2024-01-01T00:00:00Z", "eo:cloud_cover": cloud},
                    "links": [{"rel": "self", "href": f"https://catalog.example/items/{name}"}],
                    "assets": {"visual": {"href": f"https://assets.example/{name}.tif", "type": "image/tiff"}}}

        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(405)
            if request.url.path == "/page2":
                return httpx.Response(200, json={"features": [feature("clear", 5)], "links": []})
            return httpx.Response(200, json={"features": [feature("cloudy", 95)],
                "links": [{"rel": "next", "href": "https://catalog.example/page2"}]})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(service, "_validate_public_http_url"):
            scenes, truncated = service._stac_api_search(client, "https://catalog.example/search",
                {"max_cloud_cover": 20, "stac_collections": "sentinel-2-l2a"},
                service.get_imagery_provider_meta("stac_catalog"), [0, 0, 1, 1], "2024-01-01/2024-01-02", None)
        self.assertEqual(len(scenes), 1)
        self.assertTrue(scenes[0]["id"].endswith("clear"))
        self.assertFalse(truncated)
        self.assertEqual(calls[1].url.params["bbox"], "0,0,1,1")
        self.assertEqual([r.method for r in calls], ["POST", "GET", "GET"])

    def test_invalid_render_settings(self):
        for value in ("nan,3000", "0,inf", "20,10", "0,1|0,1"):
            with self.subTest(value=value), self.assertRaises(AnalysisError):
                service._parse_cog_rescale(value, 3)
        for value in ("0", "1,2", "1,2,3,4", "red"):
            with self.subTest(value=value), self.assertRaises(AnalysisError):
                service._parse_cog_bands(value)

    def test_rescale_preserves_nodata_alpha(self):
        from rio_tiler.models import ImageData
        pixels = np.ma.array(np.full((3, 16, 16), 1000, dtype="uint16"), mask=False)
        pixels.mask[:, 0, 0] = True
        source = ImageData(pixels)
        with patch.object(service, "_readable_stac_asset_href", return_value="https://assets.example/a.tif"), \
             patch.object(service, "_validate_public_http_url"), patch.object(service, "Reader") as reader:
            reader.return_value.__enter__.return_value.tile.return_value = source
            png = service._render_cog_tile("https://catalog.example/item", 0, 0, 0, "visual", None, "0,2000")
        result = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
        self.assertEqual(int(result[0, 0, 3]), 0)
        self.assertEqual(int(result[1, 1, 3]), 255)
        self.assertAlmostEqual(int(result[1, 1, 0]), 127, delta=1)

    def test_sas_is_refreshed_after_stable_asset_lookup(self):
        with patch.object(service, "_stac_asset_href", return_value="https://a.blob.core.windows.net/a.tif"), \
             patch.object(service, "_validate_public_http_url"), \
             patch("planetary_computer.sign", side_effect=["signed1", "signed2"]):
            url = "https://planetarycomputer.microsoft.com/api/stac/v1/items/a"
            self.assertEqual(service.get_stac_asset_download_url(url, "visual"), "signed1")
            self.assertEqual(service.get_stac_asset_download_url(url, "visual"), "signed2")

    def test_job_path_cannot_escape_storage(self):
        with self.assertRaises(AnalysisError):
            samgeo_service.get_job("../../.env")


if __name__ == "__main__":
    unittest.main()
