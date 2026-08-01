"""
Unit tests for the ADR-0003 reference adjustment implementation.

Pure maths — no network, no database. The reconciliation against Polygon's own
adjusted close lives in tests/integration/test_split_reconciliation.py.
"""

import datetime as dt
from decimal import Decimal

import pytest

from src.transforms.adjusted_prices import (
    Bar,
    Dividend,
    Split,
    adjust_bars,
    build_previous_session_map,
    cumulative_dividend_factor,
    cumulative_split_factor,
)

D = Decimal


def bar(day: int, close: str, volume: str = "1000", month: int = 6, year: int = 2024) -> Bar:
    """A bar with OHLC all equal to `close`, so factor effects are unambiguous."""
    price = D(close)
    return Bar(
        trading_date=dt.date(year, month, day),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=D(volume),
    )


# --------------------------------------------------------------- split factor


def test_split_factor_is_one_when_no_splits():
    assert cumulative_split_factor(dt.date(2024, 6, 7), []) == D(1)


def test_split_factor_applies_only_to_bars_before_the_ex_date():
    """The strict inequality in ADR-0003: a bar ON the ex-date is already adjusted."""
    split = Split(ex_date=dt.date(2024, 6, 10), ratio=D(10))

    assert cumulative_split_factor(dt.date(2024, 6, 7), [split]) == D(10)
    # On the ex-date itself the price already trades on the new basis.
    assert cumulative_split_factor(dt.date(2024, 6, 10), [split]) == D(1)
    assert cumulative_split_factor(dt.date(2024, 6, 11), [split]) == D(1)


def test_split_factors_compound_across_multiple_splits():
    """NVDA's real history: 4:1 in 2021, 10:1 in 2024. A 2020 bar sees both."""
    splits = [
        Split(ex_date=dt.date(2021, 7, 20), ratio=D(4)),
        Split(ex_date=dt.date(2024, 6, 10), ratio=D(10)),
    ]

    assert cumulative_split_factor(dt.date(2020, 1, 2), splits) == D(40)
    # Between the two splits, only the later one still applies.
    assert cumulative_split_factor(dt.date(2023, 1, 3), splits) == D(10)
    assert cumulative_split_factor(dt.date(2025, 1, 2), splits) == D(1)


def test_reverse_split_uses_a_fractional_ratio():
    """A 1-for-10 reverse split has ratio 0.1, which raises historical prices."""
    split = Split(ex_date=dt.date(2024, 6, 10), ratio=D("0.1"))
    assert cumulative_split_factor(dt.date(2024, 6, 7), [split]) == D("0.1")

    bars = [bar(7, "10.00")]
    result = adjust_bars(bars, splits=[split])
    assert result[0].split_adjusted_close == D("100")


def test_split_rejects_non_positive_ratio():
    with pytest.raises(ValueError, match="must be positive"):
        Split(ex_date=dt.date(2024, 6, 10), ratio=D(0))


# ------------------------------------------------------------ dividend factor


def test_dividend_factor_divides_by_the_previous_session_close():
    """
    A $2 dividend against a $100 prior close gives 1 - 0.02 = 0.98.

    Note the previous session is a Friday, not ex_date - 1 day: this is the case
    that makes calendar-aware lookup necessary rather than merely tidy.
    """
    ex_date = dt.date(2024, 6, 10)  # Monday
    prior = dt.date(2024, 6, 7)  # Friday

    factor = cumulative_dividend_factor(
        on=dt.date(2024, 6, 6),
        dividends=[Dividend(ex_date=ex_date, amount=D("2.00"))],
        closes={prior: D("100.00")},
        previous_session={ex_date: prior},
    )
    assert factor == D("0.98")


def test_dividend_factor_ignores_dividends_on_or_before_the_bar():
    ex_date = dt.date(2024, 6, 10)
    prior = dt.date(2024, 6, 7)
    args = dict(
        dividends=[Dividend(ex_date=ex_date, amount=D("2.00"))],
        closes={prior: D("100.00")},
        previous_session={ex_date: prior},
    )

    assert cumulative_dividend_factor(on=ex_date, **args) == D(1)
    assert cumulative_dividend_factor(on=dt.date(2024, 6, 11), **args) == D(1)


def test_dividend_with_no_reference_close_is_skipped_not_guessed():
    """
    Missing prior close -> no factor, rather than a fabricated denominator.

    Skipping understates the total-return series; inventing a denominator would
    corrupt it by an unknown amount. The first failure is at least bounded.
    """
    factor = cumulative_dividend_factor(
        on=dt.date(2024, 6, 1),
        dividends=[Dividend(ex_date=dt.date(2024, 6, 10), amount=D("2.00"))],
        closes={},  # no prior close available
        previous_session={dt.date(2024, 6, 10): dt.date(2024, 6, 7)},
    )
    assert factor == D(1)


# -------------------------------------------------------------------- adjust


def test_split_adjustment_preserves_traded_notional():
    """
    Price divides by the factor, volume multiplies by it, so price x volume is
    invariant. This is the property that makes adjusted volume meaningful.
    """
    bars = [bar(7, "1000.00", volume="500")]
    result = adjust_bars(bars, splits=[Split(dt.date(2024, 6, 10), D(10))])[0]

    assert result.split_adjusted_close == D("100")
    assert result.split_adjusted_volume == D("5000")
    assert result.split_adjusted_close * result.split_adjusted_volume == (
        result.close * result.volume
    )


def test_adjustment_leaves_raw_values_untouched():
    """Raw columns survive alongside adjusted ones, so the mart stays reconcilable."""
    bars = [bar(7, "1000.00", volume="500")]
    result = adjust_bars(bars, splits=[Split(dt.date(2024, 6, 10), D(10))])[0]

    assert result.close == D("1000.00")
    assert result.volume == D("500")


def test_most_recent_bar_is_never_adjusted():
    """
    Back-adjustment fixes the newest bar to its raw value. This is the defining
    property of the direction chosen in ADR-0003.
    """
    bars = [bar(7, "1000.00"), bar(11, "100.00")]
    result = adjust_bars(bars, splits=[Split(dt.date(2024, 6, 10), D(10))])

    latest = result[-1]
    assert latest.split_factor == D(1)
    assert latest.split_adjusted_close == latest.close


def test_split_series_is_continuous_across_the_ex_date():
    """
    The actual point of the exercise. NVDA closed at 1208.88 on 2024-06-07 and
    around 120.89 on 2024-06-10 after the 10:1 split. Raw, that is a -90% day.
    Adjusted, the two are within a normal day's move of each other.
    """
    bars = [bar(7, "1208.88"), bar(10, "120.89")]
    result = adjust_bars(bars, splits=[Split(dt.date(2024, 6, 10), D(10))])

    raw_return = (bars[1].close - bars[0].close) / bars[0].close
    assert raw_return < D("-0.89")  # the artefact we are removing

    before, after = result[0].split_adjusted_close, result[1].split_adjusted_close
    adjusted_return = (after - before) / before
    assert abs(adjusted_return) < D("0.01")


def test_total_return_and_split_bases_differ_when_dividends_exist():
    """The two series must not collapse into one — that ambiguity is the bug."""
    ex_date = dt.date(2024, 6, 10)
    prior = dt.date(2024, 6, 7)
    bars = [bar(7, "100.00"), bar(10, "98.00")]

    result = adjust_bars(
        bars,
        dividends=[Dividend(ex_date=ex_date, amount=D("2.00"))],
        previous_session={ex_date: prior},
    )

    first = result[0]
    assert first.split_adjusted_close == D("100.00")
    assert first.total_return_adjusted_close == D("98.00")  # 100 * 0.98


def test_empty_input_returns_empty():
    assert adjust_bars([], splits=[Split(dt.date(2024, 6, 10), D(10))]) == []


def test_previous_session_map_chains_sessions_not_calendar_days():
    sessions = [dt.date(2024, 6, 6), dt.date(2024, 6, 7), dt.date(2024, 6, 10)]
    mapping = build_previous_session_map(sessions)

    # Monday's predecessor is Friday, skipping the weekend entirely.
    assert mapping[dt.date(2024, 6, 10)] == dt.date(2024, 6, 7)
    # The earliest session has no predecessor within the supplied list.
    assert dt.date(2024, 6, 6) not in mapping
