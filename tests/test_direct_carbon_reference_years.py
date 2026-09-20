"""Direct reference layers must report the vintage actually displayed."""

import pytest

from app.services import carbon_service
from app.registries.carbon_dataset_registry import CARBON_DATASET_REGISTRY
from app.registries.model_compatibility import backfill_legacy_metadata
from app.services.carbon_service import get_direct_carbon_reference_layer
from app.services.gee_common import AnalysisError


def test_static_reference_keeps_its_vintage_when_requested_year_differs():
    layer = get_direct_carbon_reference_layer("WCMC_Carbon_ArcGIS", 2026)

    assert layer["requested_year"] == 2026
    assert layer["effective_year"] == layer["year"] == 2010
    assert "year=2010" in layer["tile_url"]


def test_temporal_cog_only_accepts_published_years():
    with pytest.raises(AnalysisError, match="2013 tidak tersedia"):
        get_direct_carbon_reference_layer("ESA_CCI_BIOMASS_V7_COG", 2013)

    layer = get_direct_carbon_reference_layer("ESA_CCI_BIOMASS_V7_COG", 2024)
    assert layer["requested_year"] == layer["effective_year"] == 2024
    assert "year=2024" in layer["tile_url"]
    assert 2013 not in layer["available_years"]


def test_wcmc_pool_is_combined_above_and_belowground_carbon():
    expected = "aboveground_belowground_biomass_carbon"
    assert CARBON_DATASET_REGISTRY["WCMC"]["target_pool"] == expected
    assert backfill_legacy_metadata({}, "wcmc_legacy_model")["target_pool"] == expected


def test_hansen_loss_year_mask_respects_requested_year():
    layer = get_direct_carbon_reference_layer("HANSEN_TREECOVER_AGB_PROXY", 2018)
    assert layer["effective_year"] == 2018
    assert "year=2018" in layer["tile_url"]
    assert layer["available_years"] == list(range(2000, 2024))


def test_catalog_distinguishes_static_vintage_from_selectable_years(monkeypatch):
    monkeypatch.setattr(carbon_service, "active_carbon_model_compatibility", lambda _db: {})
    monkeypatch.setattr(carbon_service.dataset_repo, "get_overrides_by_key", lambda _db, _module: {})
    catalog = {
        item["key"]: item for item in carbon_service.get_carbon_dataset_list(None, require_model=False)
    }
    assert catalog["WCMC"]["selection_year"] == 2010
    assert catalog["WCMC"]["year_selectable"] is False
    assert catalog["GEDI"]["year_selectable"] is False
    assert catalog["HANSEN_TREECOVER_AGB_PROXY"]["year_selectable"] is True
    assert 2013 not in catalog["ESA_CCI_BIOMASS_V7_COG"]["available_years"]
