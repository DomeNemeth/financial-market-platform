import logging

from fastapi import APIRouter
from sqlalchemy import text

from src.common.database import engine

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
def health_check() -> dict:
    """
    Returns stack health including database connectivity.
    Used by Docker healthchecks and monitoring.
    """
    db_status = "unknown"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
        logger.error(f"Health check DB failure: {e}")

    return {
        "status": "ok" if db_status == "connected" else "degraded",
        "db": db_status,
    }