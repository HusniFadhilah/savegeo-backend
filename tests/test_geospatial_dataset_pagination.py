from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.routes import geospatial
from app.db.models.geospatial_dataset import GeospatialDataset


def test_dataset_listing_filters_and_pages_in_database(monkeypatch):
    engine = create_engine("sqlite://")
    GeospatialDataset.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    rows = [
        {"id": "a", "name": "Forest A", "module": "carbon", "format": "cog"},
        {"id": "b", "name": "Forest B", "module": "carbon", "format": "cog"},
        {"id": "c", "name": "Forest C", "module": "carbon", "format": "cog"},
        {"id": "d", "name": "Water D", "module": "water", "format": "cog"},
    ]
    monkeypatch.setattr(geospatial, "viewer_feature_codes", lambda _db, _viewer: {"app.feature.carbon_stock"})
    try:
        with factory() as db:
            db.add_all([
                GeospatialDataset(id=row["id"], document=row, created_by="admin:1")
                for row in rows
            ])
            db.commit()
            result = geospatial.list_cloud_datasets(
                module=None,
                format="cog",
                search="forest",
                page=2,
                page_size=1,
                viewer=SimpleNamespace(id=1),
                db=db,
            )
    finally:
        engine.dispose()

    assert result == {
        "datasets": [rows[1]],
        "count": 3,
        "page": 2,
        "page_size": 1,
    }
