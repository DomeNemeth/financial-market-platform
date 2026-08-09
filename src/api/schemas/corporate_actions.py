"""
Response schemas for the corporate-actions endpoint.

Same Decimal-as-JSON-string rule as `prices.py`, for the same ADR-0009 §5
reason: these are the numbers the adjustment factors are built from, and
emitting a dividend of 1.25 as an IEEE-754 double here while serving the
resulting adjusted close as an exact decimal would be an odd place to lose
precision.
"""

import datetime as dt
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CorporateAction(BaseModel):
    """
    One (security_id, ex_date) event and the factor it contributed.

    The grain is the ex-date, not the action type. A security that pays a
    dividend and splits on the same date is ONE row with both legs populated —
    which mirrors `int_corporate_actions__factors` exactly, and is what stops a
    consumer double-counting the date the way a naive union of two action tables
    would.

    Both factors default to 1, the multiplicative identity, when their action
    type is absent. A dividend-only event carries `split_ratio: "1"` rather than
    null, so a consumer multiplying the columns never has to special-case a
    missing leg.
    """

    model_config = ConfigDict(from_attributes=True)

    ex_date: dt.date = Field(
        description="The date the security first trades without the entitlement."
    )

    split_ratio: Decimal = Field(
        description=(
            "Shares after per share before. A 10-for-1 split is 10. Exactly 1 "
            "when there is no split on this date."
        )
    )
    dividend_amount: Decimal | None = Field(
        default=None, description="Cash per share. Null when there is no dividend."
    )
    dividend_currency: str | None = None

    reference_session_date: dt.date | None = Field(
        default=None,
        description=(
            "The last exchange session STRICTLY BEFORE `ex_date`, from the "
            "trading calendar — not `ex_date - 1 day`. ADR-0003 defines the "
            "dividend factor against the previous session's close, and this "
            "dataset contains the live counterexample: JPM's 2026-07-06 ex-date "
            "has a reference session of 2026-07-02, because 2026-07-03 was the "
            "observed Independence Day holiday."
        ),
    )
    reference_close: Decimal | None = Field(
        default=None,
        description="The raw close on `reference_session_date`; the factor's denominator.",
    )

    dividend_factor: Decimal | None = Field(
        default=None,
        description=(
            "1 - dividend / reference_close. Exactly 1 when there is no "
            "dividend. NULL when a dividend at or above the previous close made "
            "the factor non-positive — the ADR-0003 addendum makes that NULL "
            "rather than clamping it, because a clamped value would be wrong by "
            "the size of the dividend and look entirely plausible."
        ),
    )

    # The two flags below distinguish states that are identical in the numbers
    # and opposite in meaning. Carried into the API rather than left in the
    # warehouse because a chart annotation that silently omits a dividend is
    # exactly as misleading as a price series that silently omits it.
    is_reference_close_missing: bool = Field(
        description=(
            "True when a dividend exists but no bar backs its reference session, "
            "so ADR-0003 applied no factor. Overwhelmingly this means the action "
            "predates the ingested price window, not that anything is wrong."
        )
    )
    is_dividend_factor_uncomputable: bool = Field(
        description=(
            "True when the dividend was at or above the previous close. Not a "
            "data error — it is what a liquidating distribution looks like — but "
            "the factor cannot enter a log product."
        )
    )

    action_ingested_at: dt.datetime | None = Field(
        default=None, description="When this event was last observed from the vendor."
    )


class CorporateActionsOut(BaseModel):
    """
    Every action for one resolved security, with the resolution stated.

    The envelope mirrors `PriceSeriesOut` deliberately: the same ticker resolved
    with the same `as_of` gives the same `security_id` in both responses, which
    is what makes it sound for a client to overlay one on the other. A consumer
    that took actions from a ticker-keyed endpoint and prices from a
    security-keyed one would be annotating one company's chart with another
    company's splits after a ticker reuse.
    """

    ticker: str = Field(description="The ticker as requested, before resolution.")
    security_id: int
    current_ticker: str | None = Field(
        default=None, description="That security's ticker today."
    )

    as_of: dt.date = Field(
        description="The valid-time date the ticker was resolved against."
    )
    start: dt.date | None = None
    end: dt.date | None = None

    action_count: int
    actions: list[CorporateAction]
