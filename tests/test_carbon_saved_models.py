"""Regression checks for the frozen model's tree format and CEDA origin."""
import numpy as np
from sklearn.tree import DecisionTreeRegressor

from app.providers.local_raster_provider import _carbon_tile_urls, _ceda_tile_url
from app.services.carbon_saved_models import tree_string


def test_v7_java_tile_uses_northwest_corner():
    urls = _carbon_tile_urls("ESA_CCI_BIOMASS_V7_COG", (110.35, -7.07, 110.45, -6.98), 2024)
    assert len(urls) == 1
    assert "N00E110_ESACCI" in urls[0]
    assert "fv7.0" in urls[0]


def test_legacy_ceda_url_keeps_existing_convention():
    assert _ceda_tile_url(-10, 110, "{tile_id}/{year}", 2020) == "S10E110/2020"


def test_tree_export_preserves_threshold_and_leaf_values():
    estimator = DecisionTreeRegressor(max_depth=1).fit(np.array([[0], [1], [2], [3]]), [1.25, 1.25, 9.125, 9.125])
    text = tree_string(estimator, ["nir"])
    assert "2) nir<=1.5" in text
    assert "3) nir>1.5" in text
    assert "1.25 *" in text
    assert "9.125 *" in text


def test_constant_tree_exports_a_terminal_root():
    estimator = DecisionTreeRegressor().fit([[0], [1]], [2.5, 2.5])
    assert tree_string(estimator, ["nir"]) == "1) root 2 0 2.5 *\n"
