import pytest
from sqlalchemy import create_engine, text

from src.common.config import settings


@pytest.fixture(scope="session")
def db_engine():
    """
    Real Postgres engine for integration-style tests.
    Requires the Docker stack to be up: docker-compose up -d
    """
    eng = create_engine(settings.postgres_dsn)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_test_pipeline_runs(request):
    """
    Delete test pipeline_runs before each test that touches the database.
    Only deletes rows where flow_name starts with 'test_' — safe to run
    against the same DB used for development.

    Tests that don't request db_engine (e.g. the adapter tests, which mock
    all HTTP) are skipped entirely. Without this guard the fixture would open
    a real connection for every test in the suite, making pure unit tests
    fail whenever Docker happens to be down.
    """
    if "db_engine" not in request.fixturenames:
        yield
        return

    engine = request.getfixturevalue("db_engine")
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM public.pipeline_runs WHERE flow_name LIKE 'test_%'")
        )
        conn.commit()
    yield