from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes.geospatial import DatasetReference, delete_cloud_dataset, register_cloud_dataset
from app.db.models.geospatial_dataset import GeospatialDataset


@pytest.fixture
def catalog_sessions():
    engine = create_engine("sqlite://")
    GeospatialDataset.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def test_geospatial_catalog_survives_session_restart_and_delete(catalog_sessions):
    reference = DatasetReference(
        id="remote-cog-1",
        name="Carbon reference",
        format="cog",
        sourceType="remote",
        module="carbon",
        url="https://example.test/carbon.tif",
    )
    admin = SimpleNamespace(id=7)

    with catalog_sessions() as db:
        saved = register_cloud_dataset(reference, admin=admin, db=db)
        assert saved["id"] == reference.id

    # A new session represents a separate request or API worker.
    with catalog_sessions() as db:
        row = db.get(GeospatialDataset, reference.id)
        assert row is not None
        assert row.document["url"] == reference.url
        assert row.created_by == "admin:7"

    with catalog_sessions() as db:
        delete_cloud_dataset(reference.id, _admin=admin, db=db)
    with catalog_sessions() as db:
        assert db.get(GeospatialDataset, reference.id) is None


def test_geospatial_catalog_duplicate_is_conflict(catalog_sessions):
    reference = DatasetReference(
        id="remote-cog-2",
        name="Carbon reference",
        format="cog",
        sourceType="remote",
        module="carbon",
    )
    admin = SimpleNamespace(id=7)
    with catalog_sessions() as db:
        register_cloud_dataset(reference, admin=admin, db=db)
    with catalog_sessions() as db, pytest.raises(HTTPException) as error:
        register_cloud_dataset(reference, admin=admin, db=db)
    assert error.value.status_code == 409

