import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.errors import register_error_handlers
from src.api.routers import corporate_actions, health, pipeline, prices, securities
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
    description=(
        "Multi-source financial data ingestion, warehousing, and API.\n\n"
        "**Ticker resolution is point-in-time.** Tickers are leased by exchanges "
        "and reassigned to unrelated companies, so every lookup resolves a ticker "
        "to a durable `security_id` against the security's list/delist window as "
        "of a date you control (`as_of`). See ADR-0007 and ADR-0009.\n\n"
        "**There is no `adjusted=true`.** Two adjusted series exist and they are "
        "not interchangeable: `split_adjusted` for charting and price levels, "
        "`total_return_adjusted` for returns. `price_type` is required so the API "
        "never has to guess which you meant. See ADR-0003.\n\n"
        "**Prices are JSON strings, not numbers.** JSON's only numeric type is an "
        "IEEE-754 double; these are decimals and are serialised losslessly. Parse "
        "them as decimals."
    ),
    version="0.8.0",
    lifespan=lifespan,
)

register_error_handlers(app)

app.include_router(health.router)
app.include_router(securities.router)
app.include_router(prices.router)
app.include_router(corporate_actions.router)
app.include_router(pipeline.router)
