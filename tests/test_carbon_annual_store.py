from datetime import date
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.models.carbon_annual_estimate import CarbonAnnualEstimate

from app.services.carbon_annual_store import estimate_identity, load_summary, save_summary


def identity(year=2026, today=date(2026, 11, 2), **overrides):
    data = {"aoi": {"region": {"scope": "island", "code": "Sumatra"}}, "reference_dataset": "SPAWN", **overrides}
    model = {"name": "national", "version": "1", "metadata_json": {"artifact_sha256": "abc"}}
    return estimate_identity(data, model, year, 2010, 2000, today=today)


def test_year_aoi_and_observation_cutoff_separate_results():
    assert identity()[0] != identity(2025)[0]
    assert identity()[0] != identity(aoi={"region": {"scope": "island", "code": "Jawa"}})[0]
    assert identity()[0] != identity(today=date(2026, 11, 3))[0]
    assert identity(2025)[0] == identity(2025, today=date(2026, 11, 3))[0]
    assert identity()[2] == date(2026, 11, 2)
    assert identity(2025)[2] == date(2025, 12, 31)


def test_changed_model_or_scale_cannot_reuse_old_summary():
    data = {"aoi": {"west": 94, "east": 142}, "reference_dataset": "SPAWN"}
    model = {"name": "national", "metadata_json": {"artifact_sha256": "abc"}}
    first = estimate_identity(data, model, 2026, 2010, 2000)[0]
    model["metadata_json"]["artifact_sha256"] = "changed"
    assert first != estimate_identity(data, model, 2026, 2010, 2000)[0]
    assert estimate_identity(data, model, 2026, 2010, 2000)[0] != estimate_identity(data, model, 2026, 2010, 3000)[0]


def test_summary_survives_map_expiry_without_storing_temporary_urls():
    db = MagicMock()
    db.get.return_value = None
    summary = {"statistics": {"mean": 55.2}, "area_info": {"total_carbon_tons": 123}, "coverage": 95}
    data = {"reference_dataset": "SPAWN"}
    assert save_summary(db, identity(), data, {"name": "national"}, 2026, 2010, summary)
    row = db.add.call_args.args[0]
    assert row.analysis_year == 2026 and row.reference_year == 2010
    assert row.summary == summary
    db.get.return_value = row
    loaded = load_summary(db, identity()[0])
    assert loaded == summary
    loaded["statistics"]["mean"] = 0
    assert row.summary["statistics"]["mean"] == 55.2


def test_database_failure_falls_back_to_computation():
    db = MagicMock()
    db.get.side_effect = RuntimeError("database unavailable")
    assert load_summary(db, identity()[0]) is None
    db.rollback.assert_called_once()


def test_yearly_summaries_roundtrip_in_separate_database_sessions():
    @compiles(JSONB, 'sqlite')
    def jsonb_sqlite(type_, compiler, **kwargs):
        return 'JSON'

    engine = create_engine('sqlite://')
    CarbonAnnualEstimate.__table__.create(engine)
    for year, mean in [(2025, 67.7), (2026, 62.98)]:
        with Session(engine) as db:
            assert save_summary(db, identity(year), {'reference_dataset': 'SPAWN'},
                                {'name': 'national'}, year, 2010,
                                {'statistics': {'mean': mean}, 'area_info': {}, 'coverage': 95})
    with Session(engine) as db:
        assert db.query(CarbonAnnualEstimate).count() == 2
        assert load_summary(db, identity(2025)[0])['statistics']['mean'] == 67.7
        assert load_summary(db, identity(2026)[0])['statistics']['mean'] == 62.98
    engine.dispose()
