"""
Reconcile the split adjustment three ways: SQL against Python, and both against
Polygon.

ADR-0003 implements the adjustment maths twice on purpose — pure Python in
src/transforms/adjusted_prices.py as a readable, directly unit-testable
reference, and dbt SQL for the pipeline — and says plainly that "the two are
reconciled by a test, and disagreement between them is a real signal". Until
Phase 3 that test did not exist: the SQL side had not been written, so the file
compared Python against Polygon and nothing else. This is the test the ADR was
describing.

Three legs, each catching something the others cannot:

  1. SQL vs Python. The dual-implementation cross-check. Both sides read the
     SAME staged bars out of Postgres, so the only thing that can differ is the
     arithmetic — not the fetch, not the rounding, not the window. This is the
     leg that would catch the mistakes that are actually plausible in the SQL:
     an inverted ratio, `>=` where ADR-0003 says `>`, a cumulative product that
     silently lost a factor to a NULL.

  2. SQL vs Polygon. The external oracle, pointed at the implementation that
     actually ships. Catches a bug the other two would agree on — most obviously
     a corporate action missing from raw.corporate_actions entirely, which both
     of our implementations would handle identically and identically wrongly.

  3. Python vs Polygon. Retained from before Phase 3. It anchors the reference
     implementation independently, so if leg 1 fails this says which side moved.

Polygon's aggregate adjustment is split-only, so it is compared against
`split_adjusted_close`, never `total_return_adjusted_close`. The dividend leg is
reconciled separately in test_total_return_reconciliation.py, which needs a
security with an in-window ex-date — KLAC's dividends all fall before the price
window.

Why KLA and not NVIDIA: NVDA's 10-for-1 on 2024-06-10 is the canonical example
and its split rows *are* ingested and asserted on elsewhere, but Polygon's free
tier caps the aggregates endpoint at two years of history and returns 403 for
mid-2024 bars. Reference endpoints (splits, dividends, ticker details) are not
capped, which is why the action is available while the prices are not. KLA's
10-for-1 on 2026-06-12 is the same event shape — a large-cap 10:1 — inside the
accessible window.

Requires the stack up, a Polygon API key, `dbt build` already run, and KLAC
prices and corporate actions ingested.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.common.config import settings
from src.common.tls import enable_system_trust_store
from src.ingestion.adapters.polygon import PolygonAdapter
from src.transforms.adjusted_prices import Bar, Split, adjust_bars

# `live_vendor` because leg 2 and leg 3 below call Polygon's aggregates endpoint
# for real. That is not incidental to this file — it is the point of it. The
# external-oracle legs are what distinguish "our two implementations agree" from
# "our two implementations are both right", and a recorded response would make
# them our own assertion with extra steps, incapable of ever catching a vendor
# restatement. So the test keeps its live call and is deselected in CI instead.
# ADR-0013 records why that trade was made in this direction.
#
# The skipif stays as well: it is what keeps a keyless local run honest, and it
# answers a different question (is a key available) from the marker (should this
# ever run unattended).
pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_vendor,
    pytest.mark.skipif(not settings.polygon_api_key, reason="POLYGON_API_KEY not set"),
]

# KLA Corporation's 10-for-1 split. A concrete, verifiable event: the close drops
# by ~90% overnight while nothing happens to anyone's wealth. Any implementation
# that reports a -90% return across this boundary is broken.
TICKER = "KLAC"
SPLIT_EX_DATE = dt.date(2026, 6, 12)
WINDOW_START = dt.date(2026, 6, 5)
WINDOW_END = dt.date(2026, 6, 19)

# Polygon publishes adjusted prices rounded, so exact equality is not the bar.
# 1e-6 relative is far tighter than any real disagreement in method would be,
# while absorbing presentation rounding.
RELATIVE_TOLERANCE = Decimal("1e-6")

# SQL vs Python is a much tighter comparison: same inputs, same definition, and
# the only licensed source of disagreement is that Postgres computes the
# cumulative product as exp(sum(ln(...))) while Python multiplies Decimals
# directly. The ADR-0003 addendum measures that error at ~2e-17 relative for a
# chain this length, so 1e-9 is eight orders of magnitude of headroom against
# arithmetic noise while still failing instantly on any real difference in
# method — those move the result by percent, not by 1e-9.
IMPLEMENTATION_TOLERANCE = Decimal("1e-9")


def _relative_difference(a: Decimal, b: Decimal) -> Decimal:
    """Relative difference against `b`, or absolute when b is zero."""
    return abs(a - b) / b if b else abs(a - b)


@pytest.fixture(scope="module", autouse=True)
def _trust_store():
    enable_system_trust_store()


@pytest.fixture(scope="module")
def adapter():
    return PolygonAdapter()


@pytest.fixture(scope="module")
def splits_from_db(db_engine):
    """Splits as actually ingested — not a literal — so ingestion is under test too."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT ex_date, split_to, split_from
                FROM raw.corporate_actions
                WHERE ticker = :ticker AND action_type = 'split'
                ORDER BY ex_date
            """),
            {"ticker": TICKER},
        ).fetchall()

    if not rows:
        pytest.skip(
            f"No {TICKER} splits in raw.corporate_actions. Run: "
            f"python -m src.ingestion.corporate_actions --tickers {TICKER} --since 2024-01-01"
        )
    return [Split(ex_date=r[0], ratio=Decimal(r[1]) / Decimal(r[2])) for r in rows]


@pytest.fixture(scope="module")
def staged_bars(db_engine):
    """
    The raw bars as the dbt DAG sees them, read from the staging model.

    Read from `staging`, not re-fetched from the API, and specifically not from
    `raw`: the staging model rounds Polygon's fractional volume to whole shares,
    so reading raw would feed Python a different volume than the SQL model gets
    and leg 1 would be comparing two things at once. Identical inputs are the
    whole point of an implementation cross-check.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT trading_date, open_price, high_price, low_price,
                       close_price, volume
                FROM staging.stg_polygon__prices
                WHERE ticker = :ticker
                  AND trading_date BETWEEN :start AND :end
                ORDER BY trading_date
            """),
            {"ticker": TICKER, "start": WINDOW_START, "end": WINDOW_END},
        ).fetchall()

    if not rows:
        pytest.skip(
            f"No staged {TICKER} bars in the window. Run: "
            f"python -m src.ingestion --tickers {TICKER} "
            f"--start 2026-06-01 --end 2026-07-31, then dbt build"
        )
    return [
        Bar(
            trading_date=r[0],
            open=Decimal(str(r[1])),
            high=Decimal(str(r[2])),
            low=Decimal(str(r[3])),
            close=Decimal(str(r[4])),
            volume=Decimal(str(r[5])),
        )
        for r in rows
    ]


@pytest.fixture(scope="module")
def python_adjusted(staged_bars, splits_from_db):
    """The reference implementation, over the staged bars."""
    return {b.trading_date: b for b in adjust_bars(staged_bars, splits=splits_from_db)}


@pytest.fixture(scope="module")
def sql_adjusted(db_engine):
    """The dbt model — the implementation that actually ships."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT trading_date, close_price, split_factor,
                       split_adjusted_open, split_adjusted_high,
                       split_adjusted_low, split_adjusted_close,
                       split_adjusted_volume
                FROM intermediate.int_prices_with_adjustments
                WHERE ticker = :ticker
                  AND trading_date BETWEEN :start AND :end
                ORDER BY trading_date
            """),
            {"ticker": TICKER, "start": WINDOW_START, "end": WINDOW_END},
        ).mappings().fetchall()

    if not rows:
        pytest.skip(
            "int_prices_with_adjustments has no rows for the window. "
            "Run: .\\scripts\\dbt.ps1 build"
        )
    return {r["trading_date"]: dict(r) for r in rows}


@pytest.fixture(scope="module")
def polygon_adjusted(adapter):
    """The external oracle: Polygon's own split-adjusted daily closes."""
    path = f"/v2/aggs/ticker/{TICKER}/range/1/day/{WINDOW_START}/{WINDOW_END}"
    data = adapter._get(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
    results = data.get("results") or []
    if not results:
        pytest.skip("Polygon returned no adjusted bars for the window")

    import pandas as pd

    return {
        pd.Timestamp(row["t"], unit="ms", tz="UTC").date(): Decimal(str(row["c"]))
        for row in results
    }


# --------------------------------------------------------------------------
# Non-vacuity guards. A test that only ever passes proves little; these fail if
# the fixtures stop containing the event the rest of the file is about.
# --------------------------------------------------------------------------


def test_the_window_actually_straddles_the_split(python_adjusted):
    """
    Guard against a vacuous pass. If the window had no pre-split bars, every
    comparison below would trivially hold and the test would prove nothing.
    """
    pre_split = [d for d in python_adjusted if d < SPLIT_EX_DATE]
    post_split = [d for d in python_adjusted if d >= SPLIT_EX_DATE]

    assert pre_split, "window contains no pre-split sessions"
    assert post_split, "window contains no post-split sessions"
    # The factor must actually differ across the boundary, or nothing is adjusted.
    assert python_adjusted[max(pre_split)].split_factor == Decimal(10)
    assert python_adjusted[min(post_split)].split_factor == Decimal(1)


def test_raw_series_has_the_discontinuity_we_are_correcting(python_adjusted):
    """Confirms the problem is real before asserting we fixed it."""
    dates = sorted(python_adjusted)
    last_pre = max(d for d in dates if d < SPLIT_EX_DATE)
    first_post = min(d for d in dates if d >= SPLIT_EX_DATE)

    raw_return = (
        python_adjusted[first_post].close - python_adjusted[last_pre].close
    ) / python_adjusted[last_pre].close
    assert raw_return < Decimal("-0.85"), "expected a ~-90% raw artefact across the split"


def test_sql_model_covers_the_same_sessions_as_the_reference(
    sql_adjusted, python_adjusted
):
    """
    A reconciliation over an empty intersection passes while proving nothing, and
    a SQL model that dropped half its rows would still agree on the half it kept.
    Both failure modes are caught here rather than in the comparison itself.
    """
    assert set(sql_adjusted) == set(python_adjusted), (
        "SQL and reference cover different sessions: "
        f"only in SQL={sorted(set(sql_adjusted) - set(python_adjusted))} "
        f"only in reference={sorted(set(python_adjusted) - set(sql_adjusted))}"
    )


# --------------------------------------------------------------------------
# Leg 1 — SQL against the Python reference. The dual-implementation check.
# --------------------------------------------------------------------------


def test_sql_split_factor_matches_the_reference(sql_adjusted, python_adjusted):
    """
    The cumulative product itself, before it touches a price.

    Checked separately from the adjusted prices because a wrong factor and a
    wrong division produce the same symptom downstream, and this distinguishes
    them on the first failure rather than the third.
    """
    mismatches = [
        f"{day}: sql={sql_adjusted[day]['split_factor']} "
        f"reference={python_adjusted[day].split_factor}"
        for day in sorted(python_adjusted)
        if _relative_difference(
            sql_adjusted[day]["split_factor"], python_adjusted[day].split_factor
        )
        > IMPLEMENTATION_TOLERANCE
    ]
    assert not mismatches, "cumulative split factor disagrees:\n" + "\n".join(mismatches)


@pytest.mark.parametrize(
    "sql_column,reference_attribute",
    [
        ("split_adjusted_open", "split_adjusted_open"),
        ("split_adjusted_high", "split_adjusted_high"),
        ("split_adjusted_low", "split_adjusted_low"),
        ("split_adjusted_close", "split_adjusted_close"),
        ("split_adjusted_volume", "split_adjusted_volume"),
    ],
)
def test_sql_adjusted_series_match_the_reference(
    sql_adjusted, python_adjusted, sql_column, reference_attribute
):
    """
    Every adjusted column, not just the close.

    Volume is included deliberately: it is the one column adjusted in the
    opposite direction, so an implementation that multiplied where it should
    divide would still pass on all four prices.
    """
    mismatches = [
        f"{day}: sql={sql_adjusted[day][sql_column]} "
        f"reference={getattr(python_adjusted[day], reference_attribute)}"
        for day in sorted(python_adjusted)
        if _relative_difference(
            sql_adjusted[day][sql_column],
            getattr(python_adjusted[day], reference_attribute),
        )
        > IMPLEMENTATION_TOLERANCE
    ]
    assert not mismatches, f"{sql_column} disagrees with the reference:\n" + "\n".join(
        mismatches
    )


def test_sql_preserves_traded_notional_across_the_split(sql_adjusted):
    """
    price x volume must survive the adjustment.

    The property ADR-0003's inverse volume adjustment exists to protect, and the
    one that fails loudly if the factor is ever applied in the same direction to
    both. Independent of the reference implementation, so it still holds if both
    are wrong in the same way.
    """
    for day, row in sorted(sql_adjusted.items()):
        raw_notional = row["close_price"] * row["split_adjusted_volume"] / row["split_factor"]
        adjusted_notional = row["split_adjusted_close"] * row["split_adjusted_volume"]
        assert _relative_difference(adjusted_notional, raw_notional) <= (
            IMPLEMENTATION_TOLERANCE
        ), f"{day}: notional not preserved ({adjusted_notional} vs {raw_notional})"


# --------------------------------------------------------------------------
# Legs 2 and 3 — against Polygon, the external oracle.
# --------------------------------------------------------------------------


def test_sql_split_adjusted_close_matches_polygon(sql_adjusted, polygon_adjusted):
    """
    The shipping implementation against the vendor's own adjusted series.

    This is the leg that catches a corporate action missing from
    raw.corporate_actions altogether — a defect both of our implementations
    would reproduce identically, so leg 1 would pass while the series was wrong.
    """
    shared = sorted(set(sql_adjusted) & set(polygon_adjusted))
    assert shared, "no overlapping sessions between the SQL model and Polygon"

    mismatches = [
        f"{day}: sql={sql_adjusted[day]['split_adjusted_close']} polygon={polygon_adjusted[day]}"
        for day in shared
        if _relative_difference(
            sql_adjusted[day]["split_adjusted_close"], polygon_adjusted[day]
        )
        > RELATIVE_TOLERANCE
    ]
    assert not mismatches, "SQL split-adjusted close disagrees with Polygon:\n" + "\n".join(
        mismatches
    )


def test_reference_split_adjusted_close_matches_polygon(python_adjusted, polygon_adjusted):
    """
    The reference implementation against the same oracle.

    Retained from before the SQL model existed. Its value now is diagnostic: if
    leg 1 fails, this says whether the reference or the SQL moved.
    """
    shared = sorted(set(python_adjusted) & set(polygon_adjusted))
    assert shared, "no overlapping sessions between our bars and Polygon's"

    mismatches = [
        f"{day}: ours={python_adjusted[day].split_adjusted_close} polygon={polygon_adjusted[day]}"
        for day in shared
        if _relative_difference(
            python_adjusted[day].split_adjusted_close, polygon_adjusted[day]
        )
        > RELATIVE_TOLERANCE
    ]
    assert not mismatches, "split-adjusted close disagrees with Polygon:\n" + "\n".join(
        mismatches
    )


def test_adjusted_series_is_continuous_across_the_split(sql_adjusted):
    """The property that matters downstream: no fake -90% day survives."""
    dates = sorted(sql_adjusted)
    last_pre = max(d for d in dates if d < SPLIT_EX_DATE)
    first_post = min(d for d in dates if d >= SPLIT_EX_DATE)

    before = sql_adjusted[last_pre]["split_adjusted_close"]
    after = sql_adjusted[first_post]["split_adjusted_close"]
    adjusted_return = (after - before) / before

    assert abs(adjusted_return) < Decimal("0.10"), (
        f"adjusted return across the split was {adjusted_return}, "
        "which is too large to be a normal session move"
    )
