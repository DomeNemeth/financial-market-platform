import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.common.config import settings


@pytest.fixture(scope="session")
def db_engine():
    """
    Real Postgres engine for integration-style tests.
    Requires Docker to be running: make up
    """
    eng = create_engine(settings.postgres_dsn)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_test_pipeline_runs(db_engine):
    """
    Delete test pipeline_runs before each test so tests don't interfere.
    Only deletes rows where flow_name starts with 'test_' — safe to run
    against the same DB used for development.
    """
    with db_engine.connect() as conn:
        conn.execute(
            text("DELETE FROM public.pipeline_runs WHERE flow_name LIKE 'test_%'")
        )
        conn.commit()
    yield