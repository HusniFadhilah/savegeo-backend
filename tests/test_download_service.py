from unittest.mock import MagicMock, patch

import pytest

from app.services import download_service
from app.services.gee_common import AnalysisError


@pytest.mark.parametrize("automatic", [True, False])
def test_large_pool_export_preserves_source_and_exposes_resolution(automatic):
    image = MagicMock()
    image.clip.return_value = image
    image.rename.return_value = image
    image.toFloat.return_value = image
    image.getDownloadURL.return_value = "https://example.test/result.tif"
    roi = MagicMock()
    roi.bounds.return_value.coordinates.return_value.getInfo.return_value = [
        [[0, 0], [2000000, 0], [2000000, 2000000], [0, 2000000], [0, 0]]
    ]
    roi.bounds.return_value.getInfo.return_value = {"coordinates": [[[110, -3], [115, -3], [115, 0], [110, -3]]]}
    request = {"aoi": {"west": 110, "south": -3, "east": 115, "north": 0},
               "layer_type": "carbon", "reference_dataset": "ORNL_BGB", "dataset_year": 2010,
               "reference_only": True, "year": 2026, "scale": 10, "auto_scale": automatic}
    with patch.object(download_service, "create_geometry_from_payload", return_value=roi), \
         patch.object(download_service.config_service, "get_analysis_defaults", return_value={"cloud_threshold": 10}), \
         patch.object(download_service.ee, "Projection", create=True), \
         patch("app.services.carbon_pool_service.build_pool_export_image", return_value=image) as builder:
        if not automatic:
            with pytest.raises(AnalysisError, match="minimal"):
                download_service.download_geotiff(MagicMock(), request)
            image.getDownloadURL.assert_not_called()
            return
        result = download_service.download_geotiff(MagicMock(), request)
    assert builder.call_args.args[1]["reference_dataset"] == "ORNL_BGB"
    assert builder.call_args.args[1]["dataset_year"] == 2010
    assert result["scale"] > result["requested_scale"] == 10
    assert result["scale_adjusted"] is True
    assert result["effective_year"] == 2010
    assert result["date_range"]["end"] == "2027-01-01"
    options = image.getDownloadURL.call_args.args[0]
    assert options["scale"] == result["scale"]
    assert options["crs"] == "EPSG:6933"
    assert "scale_adjusted" not in options
