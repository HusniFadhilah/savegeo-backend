from unittest.mock import MagicMock, patch

from app.services import carbon_pool_service


def test_2010_predictors_merge_landsat7_to_cover_landsat5_acquisition_gaps():
    with patch.object(carbon_pool_service, "ee") as ee:
        carbon_pool_service._landsat_predictors(MagicMock(), 2010)
        requested = [call.args[0] for call in ee.ImageCollection.call_args_list]
        assert "LANDSAT/LT05/C02/T1_L2" in requested
        assert "LANDSAT/LE07/C02/T1_L2" in requested
        ee.ImageCollection.return_value.merge.assert_called_once()
        ee.ImageCollection.return_value.merge.return_value.filterBounds.return_value.filterDate.assert_called_once_with("2010-01-01", "2011-01-01")
