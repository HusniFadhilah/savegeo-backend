import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models.dmrv_audit import DmrvAuditEvent
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.services.dmrv import field_data
from app.services.dmrv.authz import Principal
from app.services.dmrv.errors import DmrvError
from app.services.dmrv.field_import import ImportOptions

TABLES = [
    DmrvProject.__table__, ProjectMember.__table__, DmrvAuditEvent.__table__,
    ImportBatch.__table__, SurveyPlot.__table__, SurveyVisit.__table__, TreeMeasurement.__table__,
]
OWNER_1, OWNER_2, OPERATOR, ANALYST = (Principal("admin", 1), Principal("admin", 2), Principal("user", 1), Principal("user", 2))
HEADER = "plot_code,survey_year,tree_code,species,circumference_cm,dbh_cm,height,biomass_kg,latitude,longitude\n"
GOOD = [
    "1,2024,1,Asem,70,22.28,892,205.4,-7.0527,110.4396",
    "1,2024,2,Ketapang,59.3,18.88,825,137.8,-7.0527,110.4396",
]
BAD = "1,2024,3,Johar,abc,28.33,1248,382.8,-7.0527,110.4396"
OPTIONS = ImportOptions(height_unit="cm", plot_area_m2=400.0, area_basis="assumed")


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kwargs):
    return "JSON"


def csv_bytes(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=TABLES)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add_all([DmrvProject(id="p-1", name="A"), DmrvProject(id="p-2", name="B")])
        session.add_all([
            ProjectMember(project_id="p-1", principal_type="admin", principal_id=1, role="project_owner", added_by="admin:1"),
            ProjectMember(project_id="p-2", principal_type="admin", principal_id=2, role="project_owner", added_by="admin:2"),
            ProjectMember(project_id="p-1", principal_type="user", principal_id=1, role="field_operator", added_by="admin:1"),
            ProjectMember(project_id="p-1", principal_type="user", principal_id=2, role="analyst", added_by="admin:1"),
        ])
        session.commit()
        yield session
    engine.dispose()


def count(db: Session, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def commit(db, data, *, project="p-1", principal=OWNER_1, mode="partial", options=OPTIONS, name="trees.csv"):
    return field_data.commit_import(db, principal, project, name, data, None, options, mode, "req-1")


def test_preview_writes_nothing_and_suggests_mapping(db):
    result = field_data.preview_import(db, OWNER_1, "p-1", "trees.csv", csv_bytes(*GOOD, BAD), None, OPTIONS)
    assert result["suggested_mapping"]["dbh_cm"] == "dbh_cm"
    assert len(result["sample"]) == 3
    assert result["report"]["rows_total"] == 3 and result["report"]["errors"] == 1
    assert (count(db, ImportBatch), count(db, SurveyPlot), count(db, TreeMeasurement)) == (0, 0, 0)


def test_atomic_import_with_one_bad_row_saves_nothing_but_the_batch(db):
    result = commit(db, csv_bytes(*GOOD, BAD), mode="atomic")
    assert (result["status"], result["rows_imported"], result["rows_rejected"]) == ("rejected", 0, 1)
    assert (count(db, SurveyPlot), count(db, SurveyVisit), count(db, TreeMeasurement)) == (0, 0, 0)
    batch = db.get(ImportBatch, result["batch_id"])
    assert batch.status == "rejected" and batch.summary["rows_rejected"] == 1


def test_partial_import_saves_good_rows_and_reports_bad(db):
    result = commit(db, csv_bytes(*GOOD, BAD))
    assert (result["status"], result["rows_imported"], result["rows_rejected"]) == ("partial", 2, 1)
    assert (result["plots_created"], result["visits_created"]) == (1, 1)
    assert [(i["row"], i["code"]) for i in result["issues"] if i["level"] == "error"] == [(4, "bad_number")]
    tree = db.execute(select(TreeMeasurement).where(TreeMeasurement.tree_code == "1")).scalar_one()
    assert tree.height_m == pytest.approx(8.92) and tree.biomass_source == "reported"
    audit = db.execute(select(DmrvAuditEvent)).scalar_one()
    assert audit.action == "field_import.commit" and audit.project_id == "p-1" and len(audit.snapshot_hash) == 64


def test_clean_import_is_committed(db):
    assert commit(db, csv_bytes(*GOOD), mode="atomic")["status"] == "committed"


def test_second_visit_to_same_plot_keeps_first_visit(db):
    commit(db, csv_bytes(*GOOD))
    older = csv_bytes("1,2022,1,Asem,60,19.1,700,150.0,-7.0527,110.4396")
    result = commit(db, older)
    assert (result["plots_created"], result["visits_created"], result["status"]) == (0, 1, "committed")
    assert (count(db, SurveyPlot), count(db, SurveyVisit), count(db, TreeMeasurement)) == (1, 2, 3)
    visits = field_data.list_visits(db, OWNER_1, "p-1", "1")
    assert [(v["visit_date"], v["date_precision"], v["tree_count"]) for v in visits] == [
        ("2022-01-01", "year", 1), ("2024-01-01", "year", 2),
    ]


def test_same_visit_twice_is_visit_exists_error_and_changes_nothing(db):
    commit(db, csv_bytes(*GOOD))
    again = commit(db, csv_bytes(*GOOD))
    assert again["status"] == "rejected" and again["rows_imported"] == 0
    assert {i["code"] for i in again["issues"] if i["level"] == "error"} == {"visit_exists"}
    assert (count(db, SurveyVisit), count(db, TreeMeasurement)) == (1, 2)


def test_same_plot_code_in_two_projects_is_independent(db):
    commit(db, csv_bytes(*GOOD))
    commit(db, csv_bytes(*GOOD), project="p-2", principal=OWNER_2)
    assert count(db, SurveyPlot) == 2
    assert [p["plot_code"] for p in field_data.list_plots(db, OWNER_2, "p-2")] == ["1"]


def test_audit_failure_rolls_back_the_whole_import(db, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.services.dmrv.field_data.record_audit", boom)
    with pytest.raises(RuntimeError):
        commit(db, csv_bytes(*GOOD))
    db.rollback()
    assert (count(db, ImportBatch), count(db, SurveyPlot), count(db, SurveyVisit), count(db, TreeMeasurement)) == (0, 0, 0, 0)


def test_non_member_is_not_found_and_analyst_is_forbidden(db):
    with pytest.raises(DmrvError) as not_member:
        commit(db, csv_bytes(*GOOD), principal=OWNER_2)
    assert not_member.value.status == 404
    with pytest.raises(DmrvError) as analyst:
        commit(db, csv_bytes(*GOOD), principal=ANALYST)
    assert analyst.value.status == 403
    assert count(db, ImportBatch) == 0
    assert commit(db, csv_bytes(*GOOD), principal=OPERATOR)["status"] == "committed"


def test_unreadable_file_becomes_422_with_code(db):
    with pytest.raises(DmrvError) as info:
        field_data.preview_import(db, OWNER_1, "p-1", "trees.txt", b"x", None, OPTIONS)
    assert (info.value.status, info.value.code) == (422, "unsupported_file")


def test_area_conflict_keeps_stored_value_and_warns(db):
    commit(db, csv_bytes(*GOOD))
    result = commit(db, csv_bytes("1,2022,1,Asem,60,19.1,700,150.0,-7.0527,110.4396"),
                    options=ImportOptions(height_unit="cm", plot_area_m2=500.0, area_basis="measured"))
    assert any(i["code"] == "area_conflict" and i["level"] == "warning" for i in result["issues"])
    assert db.execute(select(SurveyPlot.area_m2)).scalar_one() == 400.0


def test_list_plots_and_visit_density_carry_area_basis(db):
    commit(db, csv_bytes(*GOOD))
    plots = field_data.list_plots(db, OWNER_1, "p-1")
    assert plots == [{"plot_code": "1", "latitude": -7.0527, "longitude": 110.4396, "area_m2": 400.0,
                      "area_basis": "assumed", "visit_count": 1}]
    density = field_data.list_visits(db, OWNER_1, "p-1", "1")[0]["density"]
    assert density["area_basis"] == "assumed" and density["blockers"] == []
    assert density["carbon_mg_c_per_ha"] == pytest.approx((205.4 + 137.8) / 1000 / 0.04 * 0.47, rel=1e-6)


def test_density_is_blocked_when_plot_area_unknown(db):
    commit(db, csv_bytes(*GOOD), options=ImportOptions(height_unit="cm"))
    density = field_data.list_visits(db, OWNER_1, "p-1", "1")[0]["density"]
    assert density["carbon_mg_c_per_ha"] is None and density["blockers"] == ["plot_area_unknown"]


def test_unknown_plot_in_known_project_is_not_found(db):
    with pytest.raises(DmrvError) as info:
        field_data.list_visits(db, OWNER_1, "p-1", "nope")
    assert info.value.status == 404
