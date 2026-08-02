"""
Reconcile the total-return (dividend-compounded) adjustment: SQL against the
Python reference, plus the definitional property neither vendor will check.

The split reconciliation next door has an external oracle — Polygon publishes a
split-adjusted series, so a third party can be asked whether we are right. This
one does not, and that is not an oversight: Polygon's aggregate adjustment is
split-only, so there is nothing on the free tier to compare a total-return series
against. ADR-0003's warning that "correctness is bounded by corporate-action
completeness" bites hardest exactly here.

What replaces the oracle is a property that follows from the definition rather
than from any implementation:

    total_return_adjusted_close(reference_session) == close - dividend_amount

On the session immediately before an ex-date, the dividend factor is
(1 - amount/close) and that bar's close is the very `close` in the denominator,
so the product collapses to close - amount exactly. It is checked to the cent
against arithmetic done outside both implementations, and it fails if the factor
is inverted, applied on the wrong side of the ex-date, or divided rather than
multiplied. That is most of the plausible mistakes.

Two securities, deliberately:
  - NVDA, a $0.25 dividend on 2026-06-04. Small relative to a ~$215 close, so the
    factor is 0.9988 and a sign error is a fraction of a percent — the kind of
    wrongness that hides.
  - JPM, a $1.50 dividend on 2026-07-06, whose reference session is 2026-07-02.
    2026-07-03 is the observed Independence Day holiday and the 4th and 5th are
    the weekend, so `ex_date - 1 day` would read Sunday 2026-07-05, find no bar,
    and silently drop the dividend from the product. This ticker is in the test
    specifically because it is the case ADR-0003 requires the trading calendar
    for, and a calendar regression would show up here as a factor of exactly 1.

Requires the stack up and `dbt build` already run. No network and no API key —
everything compared here is already in Postgres.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.common.calendar import previous_trading_day
from src.transforms.adjusted_prices import Bar, Dividend, Split, adjust_bars

pytestmark = pytest.mark.integration

# The full ingested range, used for BOTH implementations. Not a narrower window:
# the SQL model resolves a dividend's reference close against every bar it holds,
# so feeding Python a shorter series could make it skip a dividend the SQL
# applied, and the resulting mismatch would be an artefact of the fixture rather
# than a real disagreement.
WINDOW_START = dt.date(2026, 6, 1)
WINDOW_END = dt.date(2026, 7, 31)

# See the ADR-0003 addendum: exp(sum(ln(...))) costs ~2e-17 relative on chains
# this length, so 1e-9 cannot fail on arithmetic noise but fails instantly on any
# real difference in method.
IMPLEMENTATION_TOLERANCE = Decimal("1e-9")

# The definitional check compares money, so it is checked in money: a tenth of a
# cent on a three-figure share price.
CENT_TOLERANCE = Decimal("0.001")

# (ticker, ex_date, amount) — asserted against the database rather than trusted,
# so the test fails loudly if the underlying action ever changes.
DIVIDEND_CASES = [
    ("NVDA", dt.date(2026, 6, 4), Decimal("0.25")),
    ("JPM", dt.date(2026, 7, 6), Decimal("1.50")),
]
TICKERS = [case[0] for case in DIVIDEND_CASES]


def _relative_difference(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / b if b else abs(a - b)


@pytest.fixture(scope="module")
def actions_by_ticker(db_engine):
    """Splits and dividends as actually ingested, so ingestion is under test too."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker, action_type, ex_date, split_to, split_from, cash_amount
                FROM raw.corporate_actions
                WHERE ticker = ANY(:tickers)
                ORDER BY ticker, ex_date
            """),
            {"tickers": TICKERS},
        ).fetchall()

    result: dict[str, dict[str, list]] = {
        t: {"splits": [], "dividends": []} for t in TICKERS
    }
    for ticker, action_type, ex_date, split_to, split_from, cash_amount in rows:
        if action_type == "split":
            result[ticker]["splits"].append(
                Split(ex_date=ex_date, ratio=Decimal(split_to) / Decimal(split_from))
            )
        else:
            result[ticker]["dividends"].append(
                Dividend(ex_date=ex_date, amount=Decimal(cash_amount))
            )
    return result


@pytest.fixture(scope="module")
def staged_bars_by_ticker(db_engine):
    """The staged bars — the same input the dbt DAG consumes."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker, trading_date, open_price, high_price, low_price,
                       close_price, volume
                FROM staging.stg_polygon__prices
                WHERE ticker = ANY(:tickers)
                  AND trading_date BETWEEN :start AND :end
                ORDER BY ticker, trading_date
            """),
            {"tickers": TICKERS, "start": WINDOW_START, "end": WINDOW_END},
        ).fetchall()

    result: dict[str, list[Bar]] = {t: [] for t in TICKERS}
    for r in rows:
        result[r[0]].append(
            Bar(
                trading_date=r[1],
                open=Decimal(str(r[2])),
                high=Decimal(str(r[3])),
                low=Decimal(str(r[4])),
                close=Decimal(str(r[5])),
                volume=Decimal(str(r[6])),
            )
        )
    for ticker, bars in result.items():
        if not bars:
            pytest.skip(f"No staged bars for {ticker}; run ingestion then dbt build")
    return result


@pytest.fixture(scope="module")
def python_adjusted(staged_bars_by_ticker, actions_by_ticker):
    """
    The reference implementation, dividends included.

    The previous-session map comes from src.common.calendar.previous_trading_day
    rather than from build_previous_session_map, on that helper's own advice:
    it knows about sessions outside the loaded bar range, so an ex-date at the
    very start of the window still resolves. This mirrors what the SQL does with
    the calendar seed.
    """
    out = {}
    for ticker, bars in staged_bars_by_ticker.items():
        dividends = actions_by_ticker[ticker]["dividends"]
        previous_session = {d.ex_date: previous_trading_day(d.ex_date) for d in dividends}
        out[ticker] = {
            b.trading_date: b
            for b in adjust_bars(
                bars,
                splits=actions_by_ticker[ticker]["splits"],
                dividends=dividends,
                previous_session=previous_session,
            )
        }
    return out


@pytest.fixture(scope="module")
def sql_adjusted(db_engine):
    """The dbt model."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ticker, trading_date, close_price, split_factor,
                       dividend_factor, total_return_adjusted_close,
                       split_adjusted_close
                FROM intermediate.int_prices_with_adjustments
                WHERE ticker = ANY(:tickers)
                  AND trading_date BETWEEN :start AND :end
                ORDER BY ticker, trading_date
            """),
            {"tickers": TICKERS, "start": WINDOW_START, "end": WINDOW_END},
        ).mappings().fetchall()

    if not rows:
        pytest.skip("int_prices_with_adjustments is empty; run: .\\scripts\\dbt.ps1 build")

    out: dict[str, dict] = {t: {} for t in TICKERS}
    for r in rows:
        out[r["ticker"]][r["trading_date"]] = dict(r)
    return out


# --------------------------------------------------------------------------
# Non-vacuity guards.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ticker,ex_date,amount", DIVIDEND_CASES)
def test_the_dividend_under_test_is_really_ingested(
    actions_by_ticker, ticker, ex_date, amount
):
    """
    The whole file is vacuous if these dividends are not in the database — every
    factor would be 1 and every comparison would trivially hold.
    """
    matching = [
        d for d in actions_by_ticker[ticker]["dividends"] if d.ex_date == ex_date
    ]
    assert matching, f"{ticker} has no ingested dividend on {ex_date}"
    assert matching[0].amount == amount, (
        f"{ticker} {ex_date} dividend is {matching[0].amount}, expected {amount}; "
        "the fixture's hand-computed expectations no longer apply"
    )


@pytest.mark.parametrize("ticker,ex_date,amount", DIVIDEND_CASES)
def test_the_dividend_actually_moves_the_factor(sql_adjusted, ticker, ex_date, amount):
    """
    Guards the direction and the boundary at once: bars before the ex-date must
    carry a factor strictly below 1, and the bar ON the ex-date must be back to
    exactly 1. A factor of 1 everywhere is what a dropped dividend looks like,
    and it is the single most likely silent failure in this model.
    """
    dates = sorted(sql_adjusted[ticker])
    before = [d for d in dates if d < ex_date]
    on_or_after = [d for d in dates if d >= ex_date]
    assert before and on_or_after, f"window does not straddle {ticker} {ex_date}"

    factor_before = sql_adjusted[ticker][max(before)]["dividend_factor"]
    factor_on = sql_adjusted[ticker][min(on_or_after)]["dividend_factor"]

    assert factor_before < 1, (
        f"{ticker} dividend factor before {ex_date} is {factor_before}; "
        "the dividend never entered the cumulative product"
    )
    assert factor_on == 1, (
        f"{ticker} dividend factor on {ex_date} is {factor_on}, expected exactly 1 — "
        "a bar ON the ex-date already trades ex-dividend (ADR-0003 uses ex_date > d)"
    )


# --------------------------------------------------------------------------
# The definitional property. Independent of both implementations.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ticker,ex_date,amount", DIVIDEND_CASES)
def test_reference_session_total_return_equals_close_minus_dividend(
    sql_adjusted, ticker, ex_date, amount
):
    """
    On the session before the ex-date, total_return_adjusted_close must equal
    that session's close minus the dividend, exactly.

    This is arithmetic, not an implementation: the factor is (1 - amount/close)
    and it is applied to that same close, so the product is close - amount. It is
    the strongest statement available without an external oracle, and it fails on
    an inverted factor, a divide-instead-of-multiply, or an off-by-one on the
    ex-date boundary.

    It also pins the calendar: JPM's reference session is 2026-07-02, so if
    anything ever regresses to `ex_date - 1 day` the lookup lands on Sunday
    2026-07-05, the factor becomes 1, and this assertion is off by the full $1.50.
    """
    reference_session = previous_trading_day(ex_date)
    row = sql_adjusted[ticker].get(reference_session)
    assert row is not None, (
        f"no bar on {ticker}'s reference session {reference_session}; "
        "the dividend factor cannot have been computed from real data"
    )

    expected = row["close_price"] - amount
    actual = row["total_return_adjusted_close"]
    assert abs(actual - expected) <= CENT_TOLERANCE, (
        f"{ticker} {reference_session}: total_return={actual}, "
        f"expected close({row['close_price']}) - dividend({amount}) = {expected}"
    )


@pytest.mark.parametrize("ticker,ex_date,amount", DIVIDEND_CASES)
def test_total_return_and_split_series_differ_only_by_the_dividend(
    sql_adjusted, ticker, ex_date, amount
):
    """
    ADR-0003 refuses to publish one ambiguous `adjusted_close` because the two
    bases are different numbers. This asserts they really are different — a
    total-return series identical to the split-adjusted one would mean the
    dividend leg is not wired in at all, which every other test here could still
    pass if the factor were 1.
    """
    reference_session = previous_trading_day(ex_date)
    row = sql_adjusted[ticker][reference_session]
    assert row["split_adjusted_close"] != row["total_return_adjusted_close"], (
        f"{ticker} {reference_session}: the two adjusted bases are identical, "
        "so the dividend factor is not being applied"
    )


# --------------------------------------------------------------------------
# SQL against the Python reference.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ticker", TICKERS)
def test_sql_dividend_factor_matches_the_reference(sql_adjusted, python_adjusted, ticker):
    """The cumulative dividend product, checked before it touches a price."""
    mismatches = [
        f"{day}: sql={sql_adjusted[ticker][day]['dividend_factor']} "
        f"reference={python_adjusted[ticker][day].dividend_factor}"
        for day in sorted(python_adjusted[ticker])
        if _relative_difference(
            sql_adjusted[ticker][day]["dividend_factor"],
            python_adjusted[ticker][day].dividend_factor,
        )
        > IMPLEMENTATION_TOLERANCE
    ]
    assert not mismatches, f"{ticker} dividend factor disagrees:\n" + "\n".join(mismatches)


@pytest.mark.parametrize("ticker", TICKERS)
def test_sql_total_return_close_matches_the_reference(
    sql_adjusted, python_adjusted, ticker
):
    """The composed series — splits and dividends together."""
    assert set(sql_adjusted[ticker]) == set(python_adjusted[ticker]), (
        f"{ticker}: SQL and reference cover different sessions"
    )

    mismatches = [
        f"{day}: sql={sql_adjusted[ticker][day]['total_return_adjusted_close']} "
        f"reference={python_adjusted[ticker][day].total_return_adjusted_close}"
        for day in sorted(python_adjusted[ticker])
        if _relative_difference(
            sql_adjusted[ticker][day]["total_return_adjusted_close"],
            python_adjusted[ticker][day].total_return_adjusted_close,
        )
        > IMPLEMENTATION_TOLERANCE
    ]
    assert not mismatches, (
        f"{ticker} total_return_adjusted_close disagrees:\n" + "\n".join(mismatches)
    )
