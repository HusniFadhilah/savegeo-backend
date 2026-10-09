from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models.system_config import SystemConfig
from app.services import config_service


def test_cached_setting_is_independent_of_request_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SystemConfig.__table__.create(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    config_service.invalidate()
    try:
        with sessions() as first_session:
            first_session.add(SystemConfig(key="maps.test_key", value="cached-value", value_type="string"))
            first_session.commit()
            assert config_service.get_setting(first_session, "maps.test_key") == "cached-value"

        with sessions() as next_session:
            assert config_service.get_setting(next_session, "maps.test_key") == "cached-value"
    finally:
        config_service.invalidate()
        engine.dispose()
