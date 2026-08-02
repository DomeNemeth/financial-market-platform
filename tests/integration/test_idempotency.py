"""
Idempotency of the shared upsert path, under real duplicate pressure.

`upsert_dataframe()` is the single write path for every raw table, so this is the
one place the "re-running must not duplicate rows" guarantee is actually made.
It was previously only ever verified by hand, against a live backfill.

Two distinct duplicate sources are covered, because they fail differently:

  1. ACROSS batches — the same rows loaded twice. The failure would be silent
     row duplication, which double-counts every downstream aggregate.
  2. WITHIN one batch — two rows with the same conflict key in a single
     statement. Postgres raises "ON CONFLICT DO UPDATE command cannot affect
     row a second time" and the whole load aborts. This is what the `DISTINCT ON`
     in the upsert exists for, and vendors genuinely do it: overlapping
     paginated ranges, a restated bar.

Everything is written under a synthetic `source` so it can never collide with, or
be mistaken for, real ingested data.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import text

from src.common.database import upsert_dataframe
from src.ingestion.adapters.base import BaseAdapter

pytestmark = pytest.mark.integration

TEST_SOURCE = "test_idempotency"
TICKER = "ZZIDEM"
BASE_DATE = dt.date(2026, 6, 1)


def price_row(day_offset: int, close: str, volume: str = "1000") -> dict:
    return {
        "ticker": TICKER,
        "trading_date": BASE_DATE + dt.timedelta(days=day_offset),
        "open": Decimal(close),
        "high": Decimal(close),
        "low": Decimal(close),
        "close": Decimal(close),
        "volume": Decimal(volume),
        "vwap": Decimal(close),
        "trade_count": 100,
        "source": TEST_SOURCE,
        "ingested_at": dt.datetime.now(dt.timezone.utc),
    }


def load(rows: list[dict]) -> int:
    return upsert_dataframe(
        pd.DataFrame(rows),
        target="raw.prices",
        columns=BaseAdapter.PRICE_COLUMNS,
        conflict_columns=BaseAdapter.PRICE_CONFLICT_COLUMNS,
    )


def snapshot(conn) -> list[tuple]:
    """(id, trading_date, close) for the test rows, ordered — the identity fingerprint."""
    return conn.execute(
        text("""
            SELECT id, trading_date, close
            FROM raw.prices WHERE source = :source
            ORDER BY trading_date
        """),
        {"source": TEST_SOURCE},
    ).fetchall()


@pytest.fixture
def clean_prices(db_engine):
    def _cleanup():
        with db_engine.connect() as conn:
            conn.execute(
                text("DELETE FROM raw.prices WHERE source = :s"), {"s": TEST_SOURCE}
            )
            conn.commit()

    _cleanup()
    yield
    _cleanup()


# ------------------------------------------------------- duplicates across loads


def test_reloading_identical_rows_updates_rather_than_inserts(db_engine, clean_prices):
    """
    The core guarantee. Loading the same batch twice must leave the same rows
    with the same surrogate ids — an id change would mean a delete/insert cycle
    and would break any foreign key pointing at them.
    """
    rows = [price_row(i, f"10{i}.00") for i in range(5)]

    load(rows)
    with db_engine.connect() as conn:
        first = snapshot(conn)

    load(rows)
    with db_engine.connect() as conn:
        second = snapshot(conn)

    assert len(first) == 5
    assert first == second, "re-loading identical rows changed the stored rows"


def test_reload_does_not_allocate_new_ids(db_engine, clean_prices):
    """
    A row count alone would not catch delete-then-reinsert. Comparing the id sum
    does: ON CONFLICT DO UPDATE reuses the existing row, so ids must be stable.
    """
    rows = [price_row(i, f"10{i}.00") for i in range(5)]

    load(rows)
    with db_engine.connect() as conn:
        ids_before = {r[0] for r in snapshot(conn)}

    load(rows)
    load(rows)
    with db_engine.connect() as conn:
        ids_after = {r[0] for r in snapshot(conn)}

    assert ids_before == ids_after


def test_reload_with_changed_values_updates_in_place(db_engine, clean_prices):
    """
    A vendor restatement: same key, corrected close. The row must be updated,
    not duplicated — and the id must survive so history stays attached.
    """
    load([price_row(0, "100.00")])
    with db_engine.connect() as conn:
        original_id, _, original_close = snapshot(conn)[0]

    load([price_row(0, "999.99")])
    with db_engine.connect() as conn:
        rows = snapshot(conn)

    assert len(rows) == 1, "a restated bar created a duplicate instead of updating"
    assert rows[0][0] == original_id, "update should preserve the surrogate id"
    assert Decimal(rows[0][2]) == Decimal("999.99")
    assert Decimal(original_close) == Decimal("100.00")


# ------------------------------------------------------ duplicates within a batch


def test_intra_batch_duplicates_do_not_abort_the_load(db_engine, clean_prices):
    """
    Without DISTINCT ON this raises CardinalityViolation and the entire batch is
    lost — not just the duplicate. That is the failure mode being prevented.
    """
    rows = [price_row(0, "100.00"), price_row(0, "200.00")]

    load(rows)  # must not raise

    with db_engine.connect() as conn:
        stored = snapshot(conn)
    assert len(stored) == 1, "intra-batch duplicates should collapse to one row"


def test_intra_batch_duplicate_resolves_last_write_wins(db_engine, clean_prices):
    """
    `ORDER BY <key>, ctid DESC` keeps the physically-last staged row, so the
    winner is the last occurrence in DataFrame order. Vendors return corrections
    after originals, so last-wins is the right convention — and it must be
    deterministic, not incidental.
    """
    rows = [price_row(0, "100.00"), price_row(0, "200.00"), price_row(0, "300.00")]

    load(rows)

    with db_engine.connect() as conn:
        stored = snapshot(conn)
    assert len(stored) == 1
    assert Decimal(stored[0][2]) == Decimal("300.00"), "expected the last row to win"


def test_mixed_batch_keeps_unique_rows_and_collapses_duplicates(db_engine, clean_prices):
    """A realistic overlapping-page batch: some keys unique, some repeated."""
    rows = [
        price_row(0, "100.00"),
        price_row(1, "101.00"),
        price_row(1, "111.00"),  # duplicate of day 1
        price_row(2, "102.00"),
        price_row(0, "110.00"),  # duplicate of day 0
    ]

    load(rows)

    with db_engine.connect() as conn:
        stored = snapshot(conn)

    assert len(stored) == 3, "expected one row per distinct trading_date"
    by_date = {r[1]: Decimal(r[2]) for r in stored}
    assert by_date[BASE_DATE] == Decimal("110.00")
    assert by_date[BASE_DATE + dt.timedelta(days=1)] == Decimal("111.00")
    assert by_date[BASE_DATE + dt.timedelta(days=2)] == Decimal("102.00")


# ------------------------------------------------------------------- staging table


def test_staging_table_does_not_outlive_the_load(db_engine, clean_prices):
    """
    The staging table is TEMP with ON COMMIT DROP. A permanent one is what the
    old code left behind in `raw` — the schema dbt introspects for sources.
    """
    load([price_row(0, "100.00")])

    with db_engine.connect() as conn:
        leftover = conn.execute(
            text("""
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'raw' AND table_name LIKE '%tmp%'
                   OR table_schema = 'raw' AND table_name LIKE '_stg_%'
            """)
        ).scalar()

    assert leftover == 0, "a staging table was left behind in the raw schema"


def test_empty_dataframe_is_a_no_op(db_engine, clean_prices):
    """An empty batch (weekend, holiday) must write nothing and not raise."""
    written = upsert_dataframe(
        pd.DataFrame(columns=BaseAdapter.PRICE_COLUMNS),
        target="raw.prices",
        columns=BaseAdapter.PRICE_COLUMNS,
        conflict_columns=BaseAdapter.PRICE_CONFLICT_COLUMNS,
    )

    assert written == 0
    with db_engine.connect() as conn:
        assert snapshot(conn) == []
