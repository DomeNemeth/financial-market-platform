from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.common.config import settings

# Sync engine — fine for ingestion and Phase 1 FastAPI.
# pool_pre_ping=True tests the connection before use, catching stale connections
# after Docker restarts without crashing.
engine = create_engine(
    settings.postgres_dsn,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def check_connection() -> bool:
    """Test DB connectivity. Used by the health endpoint."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False