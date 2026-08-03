"""
The point-in-time price series endpoint. The centerpiece of the API layer.
"""

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import Connection, text

from src.api.errors import InvalidRange, RangeTooLarge
from src.api.resolution import get_connection, normalise_ticker, resolve_security
from src.api.schemas.errors import ApiError
from src.api.schemas.prices import PriceBar, PriceSeriesOut, PriceType

router = APIRouter(prefix="/prices", tags=["prices"])
logger = logging.getLogger(__name__)

# ~20 years of daily sessions. Enforced by asking for MAX_BARS + 1 rows and
# rejecting if the extra one comes back: one query, no COUNT(*), and no path
# where the response is silently truncated. A truncated series is
# indistinguishable from a complete one, which is the failure this project keeps
# refusing to ship. Pagination will replace this cap rather than join it.
MAX_BARS = 5_000

# Which mart columns back each series.
#
# The table is asymmetric because ADR-0003's methodology is asymmetric, and the
# two entries that look like omissions are deliberate:
#
#   - total_return_adjusted has NO open/high/low/vwap. ADR-0003 derives only the
#     total-return close, because a dividend factor is defined against the
#     PREVIOUS SESSION'S CLOSE and there is no defensible analogue for an
#     intraday high. They are served as explicit NULL — not omitted, and
#     emphatically not filled from the split-adjusted series. A null says "this
#     does not exist"; a substituted value would assert "here is the
#     total-return high", which is false.
#
#   - total_return_adjusted volume IS split_adjusted_volume, and that is the
#     arithmetically correct answer rather than a shortcut. A dividend does not
#     change the share count; only a split does. So the split-adjusted volume is
#     already the right volume for a total-return series.
#
# trade_count is absent from this map because it is never adjusted under any
# price_type — it counts executions, and no corporate action retroactively
# changes how many trades happened.
_SERIES_COLUMNS: dict[PriceType, dict[str, str | None]] = {
    PriceType.RAW: {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
        "volume": "volume",
        "vwap": "vwap",
    },
    PriceType.SPLIT_ADJUSTED: {
        "open": "split_adjusted_open",
        "high": "split_adjusted_high",
        "low": "split_adjusted_low",
        "close": "split_adjusted_close",
        "volume": "split_adjusted_volume",
        "vwap": "split_adjusted_vwap",
    },
    PriceType.TOTAL_RETURN_ADJUSTED: {
        "open": None,
        "high": None,
        "low": None,
        "close": "total_return_adjusted_close",
        "volume": "split_adjusted_volume",
        "vwap": None,
    },
}

# The projection below is built by string interpolation, which is safe here for
# a reason worth stating rather than assuming: the interpolated values come from
# a closed dict keyed by a Pydantic-validated enum, never from request text. An
# unknown price_type is a 422 before this code runs. Guarded at import so a
# newly added enum member cannot reach production as a KeyError at request time.
assert set(_SERIES_COLUMNS) == set(PriceType), "every PriceType needs a column mapping"


def _projection(price_type: PriceType) -> str:
    return ", ".join(
        f"{column} AS {alias}" if column else f"NULL AS {alias}"
        for alias, column in _SERIES_COLUMNS[price_type].items()
    )


@router.get(
    "/{ticker}",
    response_model=PriceSeriesOut,
    summary="Point-in-time daily price series",
    responses={
        400: {"model": ApiError, "description": "Invalid or oversized date range."},
        404: {"model": ApiError, "description": "No security held this ticker as of `as_of`."},
        409: {"model": ApiError, "description": "The ticker resolved to more than one security."},
    },
)
def get_prices(
    ticker: str = Path(description="Ticker symbol. Case-insensitive.", examples=["KLAC"]),
    price_type: PriceType = Query(
        description=(
            "REQUIRED — there is no default. `raw` is unadjusted OHLCV; "
            "`split_adjusted` is for charting and price levels; "
            "`total_return_adjusted` is for returns. ADR-0003: there is no such "
            "thing as 'the' adjusted price, so the API will not guess which you "
            "meant."
        ),
    ),
    start: dt.date | None = Query(
        default=None, description="First trading date, inclusive. Unbounded if omitted."
    ),
    end: dt.date | None = Query(
        default=None, description="Last trading date, inclusive. Unbounded if omitted."
    ),
    as_of: dt.date | None = Query(
        default=None,
        description=(
            "The date to resolve the ticker against the security's list/delist "
            "window. Defaults to `end`, or to today when `end` is omitted. It "
            "does NOT rewind the adjustment factors — see "
            "`actions_observed_through` in the response."
        ),
    ),
    conn: Connection = Depends(get_connection),
) -> PriceSeriesOut:
    """
    Daily bars for one security, in the requested series.

    Two steps, and the first is the one that matters:

    1. Resolve `ticker` -> `security_id` **as of `as_of`**, against the
       security's valid-time window. See `src/api/resolution.py` for why a bare
       ticker match here would be wrong in a way that never raises.
    2. Read the fact table by `security_id`, projecting whichever columns back
       the requested `price_type`.

    Step 2 is keyed on `security_id`, never on ticker, so a rename mid-series
    returns one continuous history for one company — and a ticker *reuse*
    returns two disjoint histories depending on `as_of`, rather than one
    fictitious merged one.

    An empty window is a 200 with `bars: []`, not a 404. The security exists;
    the range simply has no sessions in it, and conflating the two would make a
    weekend indistinguishable from a bad ticker.
    """
    if start and end and start > end:
        raise InvalidRange(
            f"start ({start.isoformat()}) is after end ({end.isoformat()})."
        )

    # `end`, not today, is the right default: asking for a delisted security's
    # 2019 prices with end=2019-12-31 must resolve identity as of 2019, when
    # that ticker belonged to that company — not as of today, when it may belong
    # to someone else entirely. Falling back to today only when the window is
    # open-ended keeps the common "give me the latest" case correct too.
    resolution_date = as_of or end or dt.date.today()

    security = resolve_security(conn, ticker, resolution_date)

    sql = text(f"""
        SELECT
            trading_date,
            {_projection(price_type)},
            trade_count,
            actions_observed_through
        FROM marts.fct_security_price_daily
        WHERE security_id = :security_id
          AND trading_date >= coalesce(CAST(:start AS date), '-infinity'::date)
          AND trading_date <= coalesce(CAST(:end   AS date),  'infinity'::date)
        ORDER BY trading_date
        LIMIT :row_limit
    """)

    rows = (
        conn.execute(
            sql,
            {
                "security_id": security["security_id"],
                "start": start,
                "end": end,
                # One over the cap: if the extra row exists, the window is too
                # wide, and we know that without a second query.
                "row_limit": MAX_BARS + 1,
            },
        )
        .mappings()
        .all()
    )

    if len(rows) > MAX_BARS:
        raise RangeTooLarge(
            f"The requested window returns more than {MAX_BARS} bars. Narrow it with "
            f"start/end. The series is rejected rather than truncated, because a "
            f"truncated series looks exactly like a complete one."
        )

    # The observation cutoff the factors were built from (ADR-0003's as_of),
    # maxed over the bars actually returned. Deliberately reported for the
    # window served rather than for the whole security: it describes THIS
    # response. Null for a security with no corporate actions, which is honest —
    # its factor of 1 rests on no observation.
    observed_through = max(
        (r["actions_observed_through"] for r in rows if r["actions_observed_through"]),
        default=None,
    )

    return PriceSeriesOut(
        ticker=normalise_ticker(ticker),
        security_id=security["security_id"],
        current_ticker=security["ticker"],
        price_type=price_type,
        as_of=resolution_date,
        start=start,
        end=end,
        bar_count=len(rows),
        actions_observed_through=observed_through,
        # Built field by field rather than **dict(r): the row also carries
        # actions_observed_through, which belongs to the envelope and not to a
        # bar. Pydantic would silently drop it, and a silent drop is a bad thing
        # to depend on.
        bars=[
            PriceBar(
                trading_date=r["trading_date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                vwap=r["vwap"],
                trade_count=r["trade_count"],
            )
            for r in rows
        ],
    )
