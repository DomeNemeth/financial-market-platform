"""Current-state security lookup over marts.dim_security."""

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import Connection

from src.api.resolution import get_connection, resolve_security
from src.api.schemas.errors import ApiError
from src.api.schemas.securities import SecurityOut

router = APIRouter(prefix="/securities", tags=["securities"])
logger = logging.getLogger(__name__)


@router.get(
    "/{ticker}",
    response_model=SecurityOut,
    summary="Look up a security by ticker",
    responses={
        404: {"model": ApiError, "description": "No security held this ticker as of `as_of`."},
        409: {"model": ApiError, "description": "The ticker resolved to more than one security."},
    },
)
def get_security(
    ticker: str = Path(description="Ticker symbol. Case-insensitive.", examples=["KLAC"]),
    as_of: dt.date | None = Query(
        default=None,
        description=(
            "Resolve against the security's list/delist window as of this date. "
            "Defaults to today, making this a current-state lookup. A DELISTED "
            "ticker therefore 404s by default and must be asked for with an "
            "as_of inside its listed window — which is the correct reading of "
            "'which security trades under this ticker today'."
        ),
    ),
    conn: Connection = Depends(get_connection),
) -> SecurityOut:
    """
    One security, resolved point-in-time.

    Thin by design — a single indexed read of the current-state dimension. It is
    thin in what it *fetches*, not in how it resolves: it goes through the same
    `resolve_security` as `/prices`, so there is exactly one implementation of
    "which security is this ticker" in the codebase and no chance of the two
    endpoints disagreeing about it.

    dim_security is filtered to the current SYSTEM-time version of each row, so
    the response describes what the platform believes *now* about a security
    that was tradeable on `as_of`. Both axes are in the response body so that
    distinction is visible rather than implied.
    """
    resolution_date = as_of or dt.date.today()
    row = resolve_security(conn, ticker, resolution_date)
    return SecurityOut(**dict(row), resolved_as_of=resolution_date)
