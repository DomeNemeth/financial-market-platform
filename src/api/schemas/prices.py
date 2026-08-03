"""
Response schemas for the price series endpoint.

EVERY PRICE IS A Decimal, NEVER A float. ADR-0003 keeps money in Decimal through
the whole Python path and in `numeric` through the whole SQL path because
adjustment factors multiply, so float error compounds along the chain. Pydantic
serialises Decimal to a JSON *string*, preserving full precision — JSON's only
numeric type is an IEEE-754 double, and emitting one here would discard that
guarantee at the exact point it becomes someone else's problem (ADR-0009 §5).
Consumers must parse these as decimals.
"""

import datetime as dt
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PriceType(str, Enum):
    """
    Which series to serve. Required on every request — there is no default.

    A boolean `adjusted=true` cannot express this. ADR-0003's central claim is
    that there is no such thing as "the" adjusted price: split_adjusted is for
    charting and price levels, total_return_adjusted is for returns, and using
    one where the other belongs yields a plausible wrong number rather than an
    error. Nothing in this platform is called `adjusted_close`.
    """

    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN_ADJUSTED = "total_return_adjusted"


class PriceBar(BaseModel):
    """
    One session, in whichever series was requested.

    The shape is identical across every `price_type`; only which fields are
    populated changes. A field that does not exist for the requested series is
    explicitly null rather than omitted or substituted — see the class docstring
    of the router's column map.
    """

    model_config = ConfigDict(from_attributes=True)

    trading_date: dt.date

    # Null for total_return_adjusted: ADR-0003 derives only the total-return
    # close, because a dividend factor is defined against the previous session's
    # close and there is no defensible analogue for an intraday high.
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None

    # Nullable even though close_price and split_adjusted_close are NOT NULL in
    # the mart. total_return_adjusted_close is null wherever a dividend at or
    # above the previous close produced a non-positive factor — the ADR-0003
    # addendum makes that NULL rather than clamping it, because a clamped value
    # would be wrong by the size of the dividend and look entirely plausible.
    close: Decimal | None = None

    # Decimal for both series so the field's type does not change with
    # price_type. Raw volume is a whole share count; split_adjusted_volume is
    # deliberately fractional (it is adjusted inversely to price so that
    # price x volume is preserved across a split).
    volume: Decimal | None = None
    vwap: Decimal | None = None

    # Never adjusted, under any price_type. It counts executions, and no
    # corporate action retroactively changes how many trades happened.
    trade_count: int | None = None


class PriceSeriesOut(BaseModel):
    """
    A resolved price series plus the provenance needed to interpret it.

    The envelope exists because the bars alone are not self-describing: which
    security they belong to is the outcome of a point-in-time resolution, and
    which corporate actions the adjusted numbers reflect is a separate fact
    again. Both are stated rather than implied.
    """

    ticker: str = Field(description="The ticker as requested, before resolution.")
    security_id: int = Field(description="The security the ticker resolved to as of `as_of`.")
    current_ticker: str | None = Field(
        default=None,
        description=(
            "That security's ticker today. Differs from `ticker` after a rename, "
            "and the difference is the audit trail for the resolution."
        ),
    )

    price_type: PriceType
    as_of: dt.date = Field(
        description=(
            "The VALID-time date the ticker was resolved against — 'which security "
            "traded under this ticker then'. Echoed because it defaults to `end`, "
            "or to today when `end` is omitted. It does NOT rewind system time and "
            "does NOT rewind the adjustment factors: see `actions_observed_through`."
        )
    )
    start: dt.date | None = Field(
        default=None, description="Requested window start; null means unbounded."
    )
    end: dt.date | None = Field(
        default=None, description="Requested window end; null means unbounded."
    )

    bar_count: int
    actions_observed_through: dt.datetime | None = Field(
        default=None,
        description=(
            "ADR-0003's `as_of` for the FACTORS: the latest observation time among "
            "the corporate actions the adjusted columns were built from. A DIFFERENT "
            "concept from the `as_of` field above, carried separately because "
            "collapsing the two is how a point-in-time claim turns out to be false. "
            "Null for a security with no corporate actions, which is honest — its "
            "factor of 1 rests on no observation. Max over the returned bars."
        ),
    )

    bars: list[PriceBar]
