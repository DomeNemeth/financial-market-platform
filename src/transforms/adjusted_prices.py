"""
Reference implementation of the price adjustment maths in ADR-0003.

This is one of two implementations on purpose. dbt SQL does the real pipeline
work; this exists to be *read* and unit-tested directly, because the maths is
subtle enough that a version you can step through in a debugger and check against
hand-computed numbers is worth the duplication. The two are reconciled by a test,
and disagreement between them is a real signal.

Optimised for legibility over speed. Factors are computed by scanning the action
list per bar rather than by an accumulating sweep — O(bars x actions) instead of
O(bars + actions). With a handful of corporate actions per security that costs
nothing, and it makes each factor a direct transcription of the definition in
ADR-0003 rather than something you have to trust an accumulator to have got right.

Decimal throughout, never float. These are money and share counts; binary floating
point cannot represent 0.01 exactly, and adjustment factors multiply, so the error
compounds along the chain rather than staying put.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

# Adjustment factors are ratios of prices, so they need more precision than the
# 6dp the price columns carry. Rounding is applied once, at the end, to results.
FACTOR_PRECISION = Decimal("0.00000000000001")


@dataclass(frozen=True)
class Split:
    """A stock split. `ratio` is split_to / split_from — NVDA 2024-06-10 is 10."""

    ex_date: dt.date
    ratio: Decimal

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError(f"Split ratio must be positive, got {self.ratio}")


@dataclass(frozen=True)
class Dividend:
    """A cash dividend, per share, in the security's own currency."""

    ex_date: dt.date
    amount: Decimal

    def __post_init__(self) -> None:
        if self.amount <= 0:
            raise ValueError(f"Dividend amount must be positive, got {self.amount}")


@dataclass(frozen=True)
class Bar:
    trading_date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class AdjustedBar:
    """
    A bar with both adjustment bases attached.

    Deliberately no field called `adjusted_close`. Splits-only and total-return
    are different numbers for different questions, and collapsing them into one
    ambiguously-named column is the exact mistake ADR-0003 exists to prevent.
    """

    trading_date: dt.date
    # Raw, exactly as the vendor sent it.
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    # The factors this bar was adjusted by, carried so a consumer can reproduce
    # or invert the adjustment without re-deriving it from the action list.
    split_factor: Decimal
    dividend_factor: Decimal
    # Splits only — the basis for charting and price-level comparison.
    split_adjusted_open: Decimal
    split_adjusted_high: Decimal
    split_adjusted_low: Decimal
    split_adjusted_close: Decimal
    split_adjusted_volume: Decimal
    # Splits and dividends — the basis for return and performance calculation.
    total_return_adjusted_close: Decimal


def cumulative_split_factor(on: dt.date, splits: Iterable[Split]) -> Decimal:
    """
    Product of every split ratio taking effect strictly after `on`.

    Strictly after: a bar ON the ex-date already trades on the post-split basis,
    so including it would double-adjust that day. This off-by-one is the single
    easiest thing to get wrong in the whole module.
    """
    factor = Decimal(1)
    for split in splits:
        if split.ex_date > on:
            factor *= split.ratio
    return factor


def cumulative_dividend_factor(
    on: dt.date,
    dividends: Iterable[Dividend],
    closes: Mapping[dt.date, Decimal],
    previous_session: Mapping[dt.date, dt.date],
) -> Decimal:
    """
    Product of (1 - amount / close_before_ex) for every dividend after `on`.

    The reference close is the close on the last *session* before the ex-date,
    which is why `previous_session` is passed in rather than computed as
    `ex_date - 1 day` — the day before an ex-date is a weekend or holiday often
    enough that date arithmetic here would produce a silently missing factor.
    That mapping comes from src.common.calendar.

    A dividend whose reference close is unavailable is skipped with no factor
    applied, since the alternative is fabricating a denominator.
    """
    factor = Decimal(1)
    for dividend in dividends:
        if dividend.ex_date <= on:
            continue
        prior = previous_session.get(dividend.ex_date)
        reference_close = closes.get(prior) if prior else None
        if not reference_close:
            continue
        factor *= Decimal(1) - (dividend.amount / reference_close)
    return factor


def adjust_bars(
    bars: Sequence[Bar],
    splits: Sequence[Split] = (),
    dividends: Sequence[Dividend] = (),
    previous_session: Mapping[dt.date, dt.date] | None = None,
) -> list[AdjustedBar]:
    """
    Back-adjust `bars` onto the most recent basis.

    Back-adjustment means the newest bar is always unchanged (its factors are 1,
    since no action lies after it) and history is restated to match. See ADR-0003
    for why this direction rather than forward-adjustment.

    Only splits and cash dividends are handled. Spin-offs, mergers, rights issues,
    and return-of-capital are out of scope and will produce a wrong series if
    present — a stated scope limit, not an oversight.
    """
    if not bars:
        return []

    closes = {bar.trading_date: bar.close for bar in bars}
    prev_session = previous_session or {}

    adjusted: list[AdjustedBar] = []
    for bar in bars:
        split_factor = cumulative_split_factor(bar.trading_date, splits)
        dividend_factor = cumulative_dividend_factor(
            bar.trading_date, dividends, closes, prev_session
        )

        # Prices divide by the split factor; volume multiplies by it. That
        # inversion is what preserves price x volume across the split, so traded
        # notional stays comparable.
        #
        # Adjusted volume is intentionally NOT rounded to a whole number: a
        # post-adjustment share count is genuinely fractional, and rounding at
        # each step compounds along a long factor chain.
        adjusted.append(
            AdjustedBar(
                trading_date=bar.trading_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                split_factor=split_factor,
                dividend_factor=dividend_factor,
                split_adjusted_open=bar.open / split_factor,
                split_adjusted_high=bar.high / split_factor,
                split_adjusted_low=bar.low / split_factor,
                split_adjusted_close=bar.close / split_factor,
                split_adjusted_volume=bar.volume * split_factor,
                total_return_adjusted_close=bar.close / split_factor * dividend_factor,
            )
        )

    return adjusted


def build_previous_session_map(sessions: Sequence[dt.date]) -> dict[dt.date, dt.date]:
    """
    Map each session to the one before it.

    A convenience for callers that already hold a session list. Production code
    should prefer src.common.calendar.previous_trading_day, which knows about
    sessions outside the range of whatever bars happen to be loaded — an ex-date
    at the very start of a window has no predecessor in `sessions` but does have
    one on the exchange.
    """
    ordered = sorted(sessions)
    return {later: earlier for earlier, later in zip(ordered, ordered[1:])}
