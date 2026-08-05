"""
Corporate actions for a resolved security.

Added in Phase 6 to support chart annotations in the dashboard. The dashboard is
a thin HTTP client with no database access of its own, and annotating a price
chart with its splits and dividends is the one thing it could not do with the
endpoints that existed. Rather than let it reach into Postgres — which would
have given it a second, unresolved way of asking "which security is this
ticker", and quietly reintroduced the bare-ticker join the whole API exists to
prevent — the capability was added here.

WHY THIS READS AN INTERMEDIATE MODEL. Every other endpoint reads `marts`. This
one reads `intermediate.int_corporate_actions__factors`, and the alternative was
a mart that would have been `select *` over that view. The model already IS the
answer to the question being asked — one row per (security_id, ex_date), each
leg reduced to the factor it contributes, with the reference session resolved
through the trading calendar. A pass-through mart would add a name and no
meaning, and would have to be kept in step with the model it copied.

The cost is real and worth stating: `intermediate` is not a published contract
the way `marts` is, so a refactor there can break this endpoint. That is
mitigated by the response schema, which pins the fields this endpoint depends
on, and by test_api_corporate_actions.py, which fails if any of them disappear.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import Connection, text

from src.api.errors import InvalidRange
from src.api.resolution import get_connection, normalise_ticker, resolve_security
from src.api.schemas.corporate_actions import CorporateAction, CorporateActionsOut
from src.api.schemas.errors import ApiError

router = APIRouter(prefix="/corporate-actions", tags=["corporate actions"])
logger = logging.getLogger(__name__)


@router.get(
    "/{ticker}",
    response_model=CorporateActionsOut,
    summary="Splits and dividends for a resolved security",
    responses={
        400: {"model": ApiError, "description": "Invalid date range."},
        404: {"model": ApiError, "description": "No security held this ticker as of `as_of`."},
        409: {"model": ApiError, "description": "The ticker resolved to more than one security."},
    },
)
def get_corporate_actions(
    ticker: str = Path(description="Ticker symbol. Case-insensitive.", examples=["KLAC"]),
    start: dt.date | None = Query(
        default=None, description="Earliest ex-date, inclusive. Unbounded if omitted."
    ),
    end: dt.date | None = Query(
        default=None, description="Latest ex-date, inclusive. Unbounded if omitted."
    ),
    as_of: dt.date | None = Query(
        default=None,
        description=(
            "The date to resolve the ticker against the security's list/delist "
            "window. Defaults to `end`, or to today when `end` is omitted — the "
            "same rule as /prices, so overlaying the two responses is sound."
        ),
    ),
    conn: Connection = Depends(get_connection),
) -> CorporateActionsOut:
    """
    Every corporate action for one security, as factors.

    Resolution is the same `resolve_security` used by `/securities` and
    `/prices`, so this endpoint inherits the 404 and 409 contracts unchanged and
    there is still exactly one implementation of "which security is this ticker"
    in the codebase.

    NO `MAX_BARS`-STYLE CAP, and that is a decision rather than an oversight.
    The cap on `/prices` exists because a 20-year daily series is 5,000 rows and
    a truncated one is indistinguishable from a complete one. Corporate actions
    are events, not observations: the densest security in this warehouse has 148
    of them across six years. An unbounded response here is bounded in practice
    by reality, and the honest guard is that there is no LIMIT at all — a silent
    truncation of an ANNOTATION set would be worse than for prices, because a
    missing split leaves a chart looking like a -90% crash with nothing marking
    it.

    A security with no actions is a 200 with `actions: []`. Most securities have
    none; that is not an error and must not look like one.
    """
    if start and end and start > end:
        raise InvalidRange(
            f"start ({start.isoformat()}) is after end ({end.isoformat()})."
        )

    resolution_date = as_of or end or dt.date.today()
    security = resolve_security(conn, ticker, resolution_date)

    # Ordered by ex_date so a client can zip this against a price series without
    # sorting it again — and because an annotation layer drawn out of order is a
    # subtle, ugly bug in the consumer rather than here.
    sql = text("""
        SELECT
            ex_date,
            split_ratio,
            dividend_amount,
            dividend_currency,
            reference_session_date,
            reference_close,
            dividend_factor,
            is_reference_close_missing,
            is_dividend_factor_uncomputable,
            action_ingested_at
        FROM intermediate.int_corporate_actions__factors
        WHERE security_id = :security_id
          AND ex_date >= coalesce(CAST(:start AS date), '-infinity'::date)
          AND ex_date <= coalesce(CAST(:end   AS date),  'infinity'::date)
        ORDER BY ex_date
    """)

    rows = (
        conn.execute(
            sql,
            {"security_id": security["security_id"], "start": start, "end": end},
        )
        .mappings()
        .all()
    )

    return CorporateActionsOut(
        ticker=normalise_ticker(ticker),
        security_id=security["security_id"],
        current_ticker=security["ticker"],
        as_of=resolution_date,
        start=start,
        end=end,
        action_count=len(rows),
        actions=[CorporateAction(**dict(r)) for r in rows],
    )
