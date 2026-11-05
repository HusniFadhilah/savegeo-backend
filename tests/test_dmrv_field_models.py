import datetime as dt
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_project import DmrvProject

FIELD_TABLES = {"dmrv_survey_plots", "dmrv_survey_visits", "dmrv_tree_measurements", "dmrv_import_batches"}
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0040_dmrv_field_data.py"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://")
    for model in (DmrvProject, ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


def _projects(db):
    db.add_all([DmrvProject(id="p-1", name="A"), DmrvProject(id="p-2", name="B")])
    db.commit()


def test_plot_code_unique_per_project_not_globally(engine):
    with Session(engine) as db:
        _projects(db)
        db.add_all([SurveyPlot(id="a", project_id="p-1", plot_code="1"), SurveyPlot(id="b", project_id="p-2", plot_code="1")])
        db.commit()
        db.add(SurveyPlot(id="c", project_id="p-1", plot_code="1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_visit_unique_per_plot_and_date(engine):
    with Session(engine) as db:
        _projects(db)
        db.add(SurveyPlot(id="a", project_id="p-1", plot_code="1"))
        db.add(SurveyVisit(id="v1", plot_id="a", visit_date=dt.date(2024, 1, 1), date_precision="year"))
        db.add(SurveyVisit(id="v2", plot_id="a", visit_date=dt.date(2022, 1, 1), date_precision="year"))
        db.commit()
        db.add(SurveyVisit(id="v3", plot_id="a", visit_date=dt.date(2024, 1, 1), date_precision="day"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_tree_unique_per_visit_and_defaults(engine):
    with Session(engine) as db:
        _projects(db)
        db.add(SurveyPlot(id="a", project_id="p-1", plot_code="1"))
        db.add(SurveyVisit(id="v1", plot_id="a", visit_date=dt.date(2024, 1, 1), date_precision="day"))
        db.add(TreeMeasurement(visit_id="v1", tree_code="1"))
        db.commit()
        tree = db.query(TreeMeasurement).one()
        assert tree.status == "live" and tree.qa_flags == [] and tree.batch_id is None
        db.add(TreeMeasurement(visit_id="v1", tree_code="1"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_migration_creates_and_drops_tables():
    spec = importlib.util.spec_from_file_location("dmrv_0040", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0040_dmrv_field_data"
    assert module.down_revision == "0039_dmrv_foundation"

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        # Foreign keys point at dmrv_projects; create a stand-in so SQLite DDL resolves.
        DmrvProject.__table__.create(conn)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert FIELD_TABLES <= set(inspect(conn).get_table_names())
        assert "batch_id" in {c["name"] for c in inspect(conn).get_columns("dmrv_tree_measurements")}
        with Operations.context(MigrationContext.configure(conn)):
            module.downgrade()
        assert not FIELD_TABLES & set(inspect(conn).get_table_names())
    engine.dispose()
