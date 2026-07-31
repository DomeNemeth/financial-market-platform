import logging

from fastapi import FastAPI

from src.api.routers import health
from src.common.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Financial Market Platform",
    description="Multi-source financial data ingestion, warehousing, and API.",
    version="0.1.0",
)

app.include_router(health.router)


@app.on_event("startup")
async def startup_event() -> None:
    logger.info("Financial Market Platform starting up")