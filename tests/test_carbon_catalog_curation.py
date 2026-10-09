"""Catalog curation must preserve scientific target identity and saved models."""
from types import SimpleNamespace

import pytest

from app.api.routes.models import models_list
from app.registries import carbon_dataset_registry as registry
from app.registries import model_compatibility
from app.services import carbon_service


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(carbon_service.dataset_repo, "get_overrides_by_key", lambda *_: {})
    monkeypatch.setattr(carbon_service, "active_carbon_model_compatibility", lambda _: {})
    monkeypatch.setattr("app.services.chloris_service.is_chloris_configured", lambda: True)
    monkeypatch.delenv("ESA_BIOMASS_2026_URL", raising=False)
    return lambda: carbon_service.get_carbon_dataset_list(None, require_model=False)


def test_catalog_excludes_duplicate_legacy_and_unconfigured_choices(catalog):
    products = {item["key"]: item for item in catalog()}
    assert len(products) == 24
    assert "GEDI" in products
    assert "GEDI_L4B_STACK" not in products
    assert "WCMC" in products
    assert "WCMC_Carbon_ArcGIS" not in products
    assert "UNEP_WCMC_Biomass" not in products
    assert "ESA_CCI_BIOMASS_V7_COG" in products
    assert not {"ESA_CCI", "ESA_CCI_SATIO_AGB", "ESA_CCI_BIOMASS_COG"} & products.keys()
    assert "GLOBAL_MANGROVE_WATCH_AGB" not in products
    assert "ESA_BIOMASS_2026" not in products
    assert "OPENLANDMAP_SOC" not in products
    # Different carbon components, depth definitions and measurement products stay distinct.
    assert {"SPAWN", "ORNL_AGB_BGB", "UNEP_WCMC_Biomass_SOC", "SOILGRIDS_OCS_0_30CM",
            "OPENLANDMAP_SOC_G_KG", "GEDI_L4A_MONTHLY", "GEDI_L4D"} <= products.keys()
    assert "SOILGRIDS_SOC_30CM" not in products


def test_configured_experimental_raster_can_be_offered(catalog, monkeypatch):
    monkeypatch.setenv("ESA_BIOMASS_2026_URL", "gs://example/verified-product.tif")
    assert "ESA_BIOMASS_2026" in {item["key"] for item in catalog()}


def test_admin_deactivation_still_wins(catalog, monkeypatch):
    monkeypatch.setattr(carbon_service.dataset_repo, "get_overrides_by_key",
                        lambda *_: {"GEDI": SimpleNamespace(is_active=False)})
    monkeypatch.setattr(carbon_service.dataset_repo, "apply_override",
                        lambda meta, override: None if override else meta)
    assert "GEDI" not in {item["key"] for item in catalog()}


class Query:
    def __init__(self, rows):
        self.rows = rows

    def filter_by(self, **_kwargs):
        return self

    def all(self):
        return self.rows


class DB:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args):
        return Query(self.rows)


def test_existing_stack_model_is_listed_under_gedi_without_double_counting():
    metadata = {"target_dataset_key": "GEDI_L4B_STACK",
                "compatible_reference_datasets": ["GEDI_L4B_STACK", "GEDI"],
                "feature_stack": "full_s2_s1_dem_dw"}
    model = SimpleNamespace(name="full_stack_model", metadata_json=metadata,
                            to_dict_public=lambda: {"name": "full_stack_model", "metadata_json": metadata})
    db = DB([model])
    assert carbon_service.active_carbon_model_compatibility(db)["GEDI"] == ["full_stack_model"]
    assert models_list(target_dataset="GEDI", db=db)["count"] == 1
    assert models_list(target_dataset="GEDI_L4B_STACK", db=db)["count"] == 1
    model_compatibility.validate_model_dataset_compatibility(metadata, "GEDI")
    assert metadata["target_dataset_key"] == "GEDI_L4B_STACK"
    assert metadata["feature_stack"] == "full_s2_s1_dem_dw"


@pytest.mark.parametrize("old,new", [
    ("ESA_CCI_BIOMASS_COG", "ESA_CCI_BIOMASS_V7_COG"),
    ("OPENLANDMAP_SOC", "OPENLANDMAP_SOC_G_KG"),
    ("SPAWN", "ORNL_AGB_BGB"),
])
def test_different_versions_scales_and_carbon_pools_are_not_model_aliases(old, new):
    with pytest.raises(ValueError, match="not compatible"):
        model_compatibility.validate_model_dataset_compatibility({"target_dataset_key": old}, new)


class Image:
    def __init__(self, value, fields=None, valid=True):
        self.value = value
        self.fields = fields or {}
        self.valid = valid

    def select(self, band):
        return Image(self.fields.get(band, self.value), self.fields, self.valid)

    def multiply(self, factor):
        return Image(self.value * factor, self.fields, self.valid)

    def rename(self, _name):
        return self

    def eq(self, value):
        return self.value == value

    def updateMask(self, valid):
        return Image(self.value, self.fields, self.valid and valid)


class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.date_ranges = []

    def filterDate(self, start, end):
        self.date_ranges.append((start, end))
        return self

    def filterBounds(self, _roi):
        return self

    def size(self):
        return SimpleNamespace(getInfo=lambda: len(self.rows))

    def map(self, fn):
        self.rows = [fn(row) for row in self.rows]
        return self

    def select(self, band):
        self.rows = [row.select(band) for row in self.rows]
        return self

    def median(self):
        import statistics
        return Image(statistics.median(row.value for row in self.rows if row.valid))


def test_openlandmap_reference_converts_units_without_reinterpreting_legacy_models():
    ee = SimpleNamespace(Image=lambda _asset: Image(20))
    physical = registry._loader_openlandmap_soc(
        registry.CARBON_DATASET_REGISTRY["OPENLANDMAP_SOC_G_KG"], 2018, None, False, ee)
    legacy = registry._loader_openlandmap_soc(
        registry.CARBON_DATASET_REGISTRY["OPENLANDMAP_SOC"], 2019, None, False, ee)
    assert physical.value == 100  # Native 20 represents 100 g/kg.
    assert legacy.value == 20
    assert registry.get_dataset_meta("OPENLANDMAP_SOC_G_KG")["unit"] == "g/kg"


def test_gedi_annual_reference_filters_quality_and_includes_december_31():
    collection = Collection([
        Image(100, {"l4_quality_flag": 1, "degrade_flag": 0}),
        Image(1000, {"l4_quality_flag": 0, "degrade_flag": 0}),
        Image(1000, {"l4_quality_flag": 1, "degrade_flag": 1}),
    ])
    ee = SimpleNamespace(ImageCollection=lambda _asset: collection)
    result = registry._loader_gedi_l4a_monthly(
        registry.CARBON_DATASET_REGISTRY["GEDI_L4A_MONTHLY"], 2025, None, False, ee)
    assert result.value == pytest.approx(47)
    assert collection.date_ranges == [("2025-01-01", "2026-01-01")]


def test_missing_gedi_year_never_loads_observations_from_other_years():
    collection = Collection([])
    ee = SimpleNamespace(ImageCollection=lambda _asset: collection)
    with pytest.raises(ValueError, match="2024 and AOI"):
        registry._loader_gedi_l4a_monthly(
            registry.CARBON_DATASET_REGISTRY["GEDI_L4A_MONTHLY"], 2024, None, False, ee)
    assert collection.date_ranges == [("2024-01-01", "2025-01-01")]


def test_new_training_choices_exclude_retired_targets():
    for key in registry.TRAINING_DATASET_KEYS:
        meta = registry.get_dataset_meta(key)
        assert not meta.get("deprecated"), key
    assert "OPENLANDMAP_SOC_G_KG" in registry.TRAINING_DATASET_KEYS
    assert "GEDI_L4B_STACK" in registry.list_dataset_keys(include_legacy=True)
