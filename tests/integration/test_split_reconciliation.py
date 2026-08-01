"""
Reconcile our split adjustment against Polygon's own adjusted close.

This is the test ADR-0003 leans on. The platform deliberately fetches
`adjusted=false` and computes adjustment itself so the logic stays ours and
auditable — but that means nothing independently checks the result. Polygon's
`adjusted=true` series is the natural oracle: an implementation that disagrees
with the vendor about a split that actually happened is wrong.

Polygon's aggregate adjustment is split-only, so it is compared against
`split_adjusted_close`, never `total_return_adjusted_close`.

The whole chain is exercised end to end: splits come from raw.corporate_actions
as ingested, not from a hardcoded literal, so a bug in ingestion fails this test
too.

Why KLA and not NVIDIA: NVDA's 10-for-1 on 2024-06-10 is the canonical example
and its split rows *are* ingested and asserted on elsewhere, but Polygon's free
tier caps the aggregates endpoint at two years of history and returns 403 for
mid-2024 bars. Reference endpoints (splits, dividends, ticker details) are not
capped, which is why the action is available while the prices are not. KLA's
10-for-1 on 2026-06-12 is the same event shape — a large-cap 10:1 — inside the
accessible window.

Requires network, a Polygon API key, and KLAC corporate actions already ingested.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import text

from src.common.config import settings
from src.common.tls import enable_system_trust_store
from src.ingestion.adapters.polygon import PolygonAdapter
from src.transforms.adjusted_prices import Bar, Split, adjust_bars

pytestmark = [
    pytest.mark.integration,
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
def polygon_adjusted(adapter):
    """The oracle: Polygon's own split-adjusted daily closes."""
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


@pytest.fixture(scope="module")
def our_adjusted(adapter, splits_from_db):
    """Our own adjustment, computed from raw (adjusted=false) bars."""
    raw = adapter.validate(adapter.fetch(TICKER, WINDOW_START, WINDOW_END))
    if raw.empty:
        pytest.skip("Polygon returned no raw bars for the window")

    bars = [
        Bar(
            trading_date=row.trading_date,
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
        )
        for row in raw.itertuples()
    ]
    return {b.trading_date: b for b in adjust_bars(bars, splits=splits_from_db)}


def test_the_window_actually_straddles_the_split(our_adjusted):
    """
    Guard against a vacuous pass. If the window had no pre-split bars, every
    comparison below would trivially hold and the test would prove nothing.
    """
    pre_split = [d for d in our_adjusted if d < SPLIT_EX_DATE]
    post_split = [d for d in our_adjusted if d >= SPLIT_EX_DATE]

    assert pre_split, "window contains no pre-split sessions"
    assert post_split, "window contains no post-split sessions"
    # The factor must actually differ across the boundary, or nothing is adjusted.
    assert our_adjusted[max(pre_split)].split_factor == Decimal(10)
    assert our_adjusted[min(post_split)].split_factor == Decimal(1)


def test_raw_series_has_the_discontinuity_we_are_correcting(our_adjusted):
    """Confirms the problem is real before asserting we fixed it."""
    dates = sorted(our_adjusted)
    last_pre = max(d for d in dates if d < SPLIT_EX_DATE)
    first_post = min(d for d in dates if d >= SPLIT_EX_DATE)

    raw_return = (
        our_adjusted[first_post].close - our_adjusted[last_pre].close
    ) / our_adjusted[last_pre].close
    assert raw_return < Decimal("-0.85"), "expected a ~-90% raw artefact across the split"


def test_split_adjusted_close_matches_polygon(our_adjusted, polygon_adjusted):
    """The reconciliation itself, on every session in the window."""
    shared = sorted(set(our_adjusted) & set(polygon_adjusted))
    assert shared, "no overlapping sessions between our bars and Polygon's"

    mismatches = []
    for day in shared:
        ours = our_adjusted[day].split_adjusted_close
        theirs = polygon_adjusted[day]
        if theirs == 0:
            continue
        if abs(ours - theirs) / theirs > RELATIVE_TOLERANCE:
            mismatches.append(f"{day}: ours={ours} polygon={theirs}")

    assert not mismatches, "split-adjusted close disagrees with Polygon:\n" + "\n".join(
        mismatches
    )


def test_adjusted_series_is_continuous_across_the_split(our_adjusted):
    """The property that matters downstream: no fake -90% day survives."""
    dates = sorted(our_adjusted)
    last_pre = max(d for d in dates if d < SPLIT_EX_DATE)
    first_post = min(d for d in dates if d >= SPLIT_EX_DATE)

    before = our_adjusted[last_pre].split_adjusted_close
    after = our_adjusted[first_post].split_adjusted_close
    adjusted_return = (after - before) / before

    assert abs(adjusted_return) < Decimal("0.10"), (
        f"adjusted return across the split was {adjusted_return}, "
        "which is too large to be a normal session move"
    )
