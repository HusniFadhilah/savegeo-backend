from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes.geospatial import DatasetReference


def _reference(url: str) -> dict:
    return {
        "id": "public-sample",
        "name": "Public sample",
        "format": "cog",
        "url": url,
        "access": "public",
        "sourceType": "remote",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/data.tif",
        "http://10.0.0.2/data.tif",
        "http://localhost/data.tif",
        "http://user:password@example.test/data.tif",
    ],
)
def test_dataset_reference_rejects_ssrf_prone_urls(url: str):
    with pytest.raises(ValidationError):
        DatasetReference.model_validate(_reference(url))


def test_dataset_reference_accepts_public_https_url():
    reference = DatasetReference.model_validate(_reference("https://data.example.test/sample.tif"))
    assert reference.url == "https://data.example.test/sample.tif"
