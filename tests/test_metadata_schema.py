from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.metadata import DatasetMetadata


def _metadata(**overrides):
    data = {
        "dataset_id": "sentinel2:2026:sample",
        "title": "Sample composite",
        "abstract": "Cloud-masked sample composite.",
        "owner": "SaveGeo",
        "provider": "Example provider",
        "source_url": "https://example.test/catalog/sample",
        "license": "Provider terms",
        "attribution": "Example provider",
        "temporal_start": "2026-01-01",
        "temporal_end": "2026-01-31",
        "bbox": [100.0, -3.0, 101.0, -2.0],
        "crs": "EPSG:4326",
        "resolution": 10,
        "scale": 10,
        "units": "reflectance",
        "nodata": -9999,
        "bands": [{"name": "B04", "description": "Red", "units": "reflectance"}],
        "acquisition_date": "2026-01-15",
        "processing_date": "2026-02-01",
        "processing_level": "L2A",
        "cloud_cover_percent": 20,
        "lineage": ["provider catalog", "cloud mask"],
        "processing_software": "savegeo/0.1.0",
        "model_version": None,
        "quality_statement": "Sample only; not for operational decisions.",
        "known_limitations": ["Example dataset"],
        "contact": "data@example.test",
        "checksum": "sha256:" + "a" * 64,
        "retention_policy": "retain while published",
    }
    data.update(overrides)
    return data


def test_dataset_metadata_accepts_reproducible_contract():
    metadata = DatasetMetadata.model_validate(_metadata())
    assert metadata.crs == "EPSG:4326"
    assert metadata.checksum.startswith("sha256:")


@pytest.mark.parametrize(
    "field,value", [("bbox", [181, -3, 182, -2]), ("resolution", 0), ("checksum", "not-a-hash")]
)
def test_dataset_metadata_rejects_invalid_spatial_or_integrity_fields(field, value):
    with pytest.raises(ValidationError):
        DatasetMetadata.model_validate(_metadata(**{field: value}))
