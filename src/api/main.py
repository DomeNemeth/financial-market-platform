import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routers import health
from src.common.database import engine
from src.common.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Startup/shutdown hooks. Replaces the deprecated @app.on_event decorators,
    which FastAPI removes in a future major.

    Everything before `yield` runs at startup, everything after at shutdown.
    Owning the pool here is the point: engine.dispose() hands pooled Postgres
    connections back on a clean exit instead of leaving the server to reap them
    when the sockets drop.
    """
    logger.info("Financial Market Platform starting up")
    yield
    logger.info("Financial Market Platform shutting down")
    engine.dispose()


app = FastAPI(
    title="Financial Market Platform",
    description="Multi-source financial data ingestion, warehousing, and API.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
