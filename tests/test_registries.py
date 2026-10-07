"""Registry/discovery tests - pure Python, no database or GEE required."""
from __future__ import annotations

from app.registries.landcover_dataset_registry import get_dataset_list
from app.registries.map_layer_registry import get_all_layers, get_basemaps
from app.registries.satellite_provider_registry import SATELLITE_PROVIDERS
from app.registries.vegetation_index_registry import get_catalog_payload


def test_satellite_is_default_basemap():
    basemaps = get_basemaps()
    assert basemaps, "expected at least one basemap"
    assert basemaps[0]["key"] == "satellite"
    assert basemaps[0].get("is_default") is True


def test_map_layers_include_carbon_layers():
    layers = get_all_layers(module="carbon")
    keys = {layer["key"] for layer in layers}
    assert {"carbon_estimated", "carbon_reference"}.issubset(keys)


def test_vegetation_catalog_payload_shape():
    payload = get_catalog_payload()
    assert "indices" in payload or "categories" in payload


def test_legacy_landsat_collections_are_selectable_for_vegetation():
    expected = {
        "landsat4": ("LANDSAT/LT04/C02/T1_L2", 1982),
        "landsat5": ("LANDSAT/LT05/C02/T1_L2", 1984),
        "landsat7": ("LANDSAT/LE07/C02/T1_L2", 1999),
    }
    assert set(expected).issubset(SATELLITE_PROVIDERS)

    for key, (collection, first_year) in expected.items():
        provider = SATELLITE_PROVIDERS[key]
        assert provider["gee_collection"] == collection
        assert provider["start_year"] == first_year
        assert provider["band_role_map"] == {
            "blue": "SR_B1",
            "green": "SR_B2",
            "red": "SR_B3",
            "nir": "SR_B4",
            "swir1": "SR_B5",
            "swir2": "SR_B7",
        }


def test_landcover_dataset_list_not_empty():
    datasets = get_dataset_list(module="landcover")
    assert len(datasets) > 0
    assert all("key" in d for d in datasets)
