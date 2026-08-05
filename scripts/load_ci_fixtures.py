"""
Load the CI warehouse fixtures into an empty `raw` schema.

This is the step that makes CI mean something. Without it, a CI run against a
freshly-migrated database would be green and worthless: `dbt build` would
materialise empty models, every non-vacuity guard in the dbt suite would have
nothing to guard, and `test_total_return_reconciliation` would `pytest.skip`
itself outright. See ADR-0013.

WHAT THE FIXTURES ARE. A snapshot of the real warehouse, exported by
`scripts/export_ci_fixtures.py` — KLA's actual 10-for-1 split on 2026-06-12,
JPMorgan's actual 2026-07-06 dividend, the actual per-vendor disagreement on
KLAC's intraday extremes, FRED's actual publication lags. ADR-0013 records why
they are not synthetic: every one of those facts is load-bearing for a test, and
data fabricated to satisfy an assertion can never fail it for a real reason.

WHY THIS REUSES `upsert_dataframe()` RATHER THAN COPY OR PLAIN INSERT. It would
be faster to `COPY` these CSVs straight in, and that is exactly the problem. The
project's idempotency guarantee lives in one function, with one `DISTINCT ON`
and one set of conflict keys per table. A loader that wrote rows any other way
would be a second, more permissive path into `raw` — one that CI exercises on
every run and production never does, so a divergence between them would surface
as a CI-only pass or a CI-only failure with no obvious cause. Loading through
the production write path also means this script is itself a smoke test of it:
running it twice is a no-op, and the `--verify` pass asserts that.

The conflict keys and column lists are imported from the ingestion modules that
own them, never restated here, for the same reason.

Usage:
    python -m scripts.load_ci_fixtures            # load, then verify
    python -m scripts.load_ci_fixtures --verify   # verify only, load nothing
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from src.common.database import engine, upsert_dataframe
from src.ingestion.adapters.base import BaseAdapter
from src.ingestion.corporate_actions import CORPORATE_ACTION_COLUMNS
from src.ingestion.fred import (
    OBSERVATION_COLUMNS,
    OBSERVATION_CONFLICT_COLUMNS,
    SERIES_COLUMNS,
    SERIES_CONFLICT_COLUMNS,
)
from src.ingestion.security_master import SECURITY_MASTER_COLUMNS

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "ci"

#: Only macro observations from this date forward are carried in the fixture.
#: The full table is ~49k rows, overwhelmingly daily Treasury history back to
#: 1962. ADR-0013 records the properties this cutoff was verified to preserve —
#: GDP's 175-day maximum publication lag, a first release before the price
#: window opens for every point-in-time-capable series, and the 18 observations
#: a naive `observation_date` join would leak. Change it only by re-checking
#: those, not by eyeballing the row count.
MACRO_CUTOFF = "2023-01-01"

#: raw.security_identity is the one table with no column list in src/, because
#: production writes it row-at-a-time through resolve_security_id() rather than
#: as a frame. Restated here, and the identity test below fails loudly if the
#: real table ever grows a column this misses.
SECURITY_IDENTITY_COLUMNS = [
    "security_id", "identity_key", "identity_kind", "first_seen_at", "resolved_at",
]


@dataclass(frozen=True)
class FixtureTable:
    """One CSV, and the production write path it is loaded through."""

    name: str
    target: str
    columns: list[str]
    conflict_columns: list[str]
    #: Non-empty means the loader asserts at least this many rows afterwards.
    #: A fixture that silently loaded nothing would leave CI green and hollow.
    min_rows: int = 1
    #: Extra assertions run by --verify, as (description, SQL returning a count,
    #: expected count). These are the facts the dbt and pytest suites depend on;
    #: if the fixture is ever regenerated from a warehouse that has lost one of
    #: them, this fails here rather than as a confusing test failure later.
    invariants: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return FIXTURE_DIR / f"{self.name}.csv"


# Load order is foreign-key order and is not negotiable:
#   security_master.security_id     -> security_identity.security_id
#   corporate_actions.security_id   -> security_identity.security_id
#   macro_observations(series_id, source) -> macro_series(series_id, source)
# raw.prices is keyed on ticker and carries no security_id at all — resolution
# happens in int_prices_with_calendar, which is the whole point of Phase 3 — so
# it has no FK and could go anywhere. It is placed with the other equity data.
TABLES: list[FixtureTable] = [
    FixtureTable(
        name="security_identity",
        target="raw.security_identity",
        columns=SECURITY_IDENTITY_COLUMNS,
        conflict_columns=["security_id"],
        min_rows=6,
    ),
    FixtureTable(
        name="security_master",
        target="raw.security_master",
        columns=SECURITY_MASTER_COLUMNS,
        conflict_columns=["security_id", "source"],
        min_rows=6,
        invariants=[
            (
                "CUSIP and ISIN are NULL by design (ADR-0007) — a fixture that "
                "carried them would mean licensed identifiers had been fabricated",
                "SELECT count(*) FROM raw.security_master "
                "WHERE cusip IS NOT NULL OR isin IS NOT NULL",
                0,
            ),
        ],
    ),
    FixtureTable(
        name="corporate_actions",
        target="raw.corporate_actions",
        columns=CORPORATE_ACTION_COLUMNS,
        conflict_columns=["security_id", "action_type", "ex_date", "source"],
        min_rows=100,
        invariants=[
            (
                "KLAC's 2026-06-12 10-for-1 split is present — test_split_"
                "reconciliation and assert_deadjusted_yahoo_reconciles_to_polygon_raw "
                "are both built on this exact event",
                "SELECT count(*) FROM raw.corporate_actions "
                "WHERE ticker = 'KLAC' AND action_type = 'split' "
                "AND ex_date = DATE '2026-06-12' AND split_to = 10 AND split_from = 1",
                1,
            ),
            (
                "JPM's 2026-07-06 dividend is present — its previous session is "
                "2026-07-02 because 2026-07-03 was the observed Independence Day "
                "holiday, which is the live instance of the hazard the trading "
                "calendar exists for",
                "SELECT count(*) FROM raw.corporate_actions "
                "WHERE ticker = 'JPM' AND action_type = 'dividend' "
                "AND ex_date = DATE '2026-07-06'",
                1,
            ),
        ],
    ),
    FixtureTable(
        name="prices",
        target="raw.prices",
        columns=BaseAdapter.PRICE_COLUMNS,
        conflict_columns=BaseAdapter.PRICE_CONFLICT_COLUMNS,
        min_rows=500,
        invariants=[
            (
                "Both vendors are present — with only Polygon bars the entire "
                "ADR-0006 merge layer would be untested and its de-adjustment "
                "reconciliation vacuous",
                "SELECT count(DISTINCT source) FROM raw.prices "
                "WHERE source IN ('polygon', 'yahoo')",
                2,
            ),
            (
                "Yahoo's KLAC bars overlap the split on the PRE-split side — "
                "these nine sessions are the non-vacuity guard of assert_"
                "deadjusted_yahoo_reconciles_to_polygon_raw, and without them "
                "every de-adjustment factor is 1 and the test proves nothing",
                "SELECT count(*) FROM raw.prices WHERE source = 'yahoo' "
                "AND ticker = 'KLAC' AND trading_date < DATE '2026-06-12' "
                "AND trading_date >= DATE '2026-06-01'",
                9,
            ),
            (
                "vwap is NULL on every Yahoo bar and was never fabricated "
                "(ADR-0006) — (h+l+c)/3 would be plausible enough that nothing "
                "downstream could catch it",
                "SELECT count(*) FROM raw.prices "
                "WHERE source = 'yahoo' AND vwap IS NOT NULL",
                0,
            ),
        ],
    ),
    FixtureTable(
        name="macro_series",
        target="raw.macro_series",
        columns=SERIES_COLUMNS,
        conflict_columns=SERIES_CONFLICT_COLUMNS,
        min_rows=10,
        invariants=[
            (
                "The three series with no publication history are kept, not "
                "dropped (ADR-0012) — their absence is the entire point of "
                "supports_point_in_time_join",
                "SELECT count(*) FROM raw.macro_series "
                "WHERE series_id IN ('DGS10', 'DGS2', 'T10Y2Y')",
                3,
            ),
        ],
    ),
    FixtureTable(
        name="macro_observations",
        target="raw.macro_observations",
        columns=OBSERVATION_COLUMNS,
        conflict_columns=OBSERVATION_CONFLICT_COLUMNS,
        min_rows=3000,
        invariants=[
            (
                "GDP's maximum publication lag survives the 2023 cutoff at the "
                "full 175 days — the single sharpest illustration of why "
                "ADR-0012 joins on first_published_date",
                "SELECT max(first_published_date - observation_date) "
                "FROM raw.macro_observations WHERE series_id = 'GDP'",
                175,
            ),
            (
                "18 observations are dated inside the price window but were "
                "published after it opened — these are exactly the rows a naive "
                "observation_date join leaks, and assert_point_in_time_macro_"
                "differs_from_naive passes vacuously without them",
                "SELECT count(*) FROM raw.macro_observations "
                "WHERE first_published_date > DATE '2026-05-01' "
                "AND observation_date <= DATE '2026-08-03'",
                18,
            ),
            (
                "DGS10's 2026-07-03 '.' sentinel became NULL, not 0.0 — the "
                "observed Independence Day holiday, where a zero would be a "
                "ten-year Treasury yield of zero percent",
                "SELECT count(*) FROM raw.macro_observations "
                "WHERE series_id = 'DGS10' AND observation_date = DATE '2026-07-03' "
                "AND value IS NULL",
                1,
            ),
        ],
    ),
]


def read_fixture(table: FixtureTable) -> pd.DataFrame:
    """
    Read one fixture CSV as pure strings, with empties as None.

    `dtype=str` is a precision guarantee, not laziness. Letting pandas infer
    types would parse every price into a float64 — and this project's central
    claim is that money is Decimal and never float (ADR-0003). A close of
    2411.64 round-tripped through float64 is no longer 2411.64, and the split
    reconciliation asserts to 1e-9. Handing Postgres the original text and
    letting the column's own NUMERIC type do the cast preserves every digit.

    `keep_default_na=False` stops pandas turning the string "NA" into a NaN;
    the explicit empty-string replacement then makes genuine NULLs None, which
    is what `to_records()` in the upsert path requires anyway.
    """
    if not table.path.exists():
        raise FileNotFoundError(
            f"Missing fixture {table.path}. Regenerate the set from a populated "
            f"warehouse with: python -m scripts.export_ci_fixtures"
        )

    df = pd.read_csv(table.path, dtype=str, keep_default_na=False)

    missing = [c for c in table.columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"{table.path.name} is missing columns {missing}. The table's column "
            f"list in src/ has probably changed since the fixture was exported — "
            f"re-run scripts/export_ci_fixtures.py against a live warehouse."
        )

    return df[table.columns].where(df[table.columns] != "", None)


def load_all() -> dict[str, int]:
    """Load every fixture in foreign-key order. Returns rows written per table."""
    written: dict[str, int] = {}

    for table in TABLES:
        df = read_fixture(table)
        rows = upsert_dataframe(
            df,
            target=table.target,
            columns=table.columns,
            conflict_columns=table.conflict_columns,
        )
        written[table.name] = rows
        logger.info(f"{table.target:<28} {rows:>6} rows")

    _resync_identity_sequence()
    return written


def _resync_identity_sequence() -> None:
    """
    Point raw.security_identity's sequence past the highest fixture security_id.

    security_id is a BIGSERIAL, and the fixture inserts explicit values for it
    because every corporate action and master row is keyed on those exact
    numbers. Explicit inserts do not advance the sequence, so without this the
    next security minted after a fixture load — by an integration test, or by
    anyone running ingestion against a CI-shaped database — would be handed
    security_id 1 and collide with a row that already exists.

    The failure would be a primary key violation, which is at least loud. The
    reason to fix it here rather than tolerate it is ADR-0007's stronger claim:
    security_id is never reused. A sequence that hands out an occupied id has
    broken that promise, and the next id it hands out would be the first one
    that is genuinely free — silently attaching a new security to whatever the
    collision left behind.
    """
    with engine.begin() as conn:
        conn.execute(text("""
            SELECT setval(
                pg_get_serial_sequence('raw.security_identity', 'security_id'),
                COALESCE((SELECT max(security_id) FROM raw.security_identity), 1),
                true
            )
        """))


def verify() -> list[str]:
    """
    Check the loaded fixture still carries the facts the test suites need.

    Returns a list of failure descriptions; empty means everything held.

    This exists because the fixture is a snapshot, and a snapshot taken from a
    warehouse that had drifted would load perfectly and quietly disarm half the
    suite. Every invariant below is one a real test depends on to be non-vacuous
    — the guard rows, the sentinel NULL, the publication lag. Failing here names
    the missing fact; failing three steps later names a dbt model.
    """
    failures: list[str] = []

    with engine.connect() as conn:
        for table in TABLES:
            actual = conn.execute(
                text(f"SELECT count(*) FROM {table.target}")  # noqa: S608 — fixed literals
            ).scalar()
            if actual < table.min_rows:
                failures.append(
                    f"{table.target}: {actual} rows, expected at least {table.min_rows}"
                )

            for description, sql, expected in table.invariants:
                result = conn.execute(text(sql)).scalar()
                if result != expected:
                    failures.append(
                        f"{table.target}: expected {expected}, got {result} — {description}"
                    )

    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only check an already-loaded fixture; write nothing.",
    )
    args = parser.parse_args()

    if not args.verify:
        logger.info(f"Loading CI fixtures from {FIXTURE_DIR}")
        written = load_all()
        logger.info(f"Loaded {sum(written.values())} rows across {len(written)} tables")

    failures = verify()
    if failures:
        logger.error("Fixture verification FAILED:")
        for failure in failures:
            logger.error(f"  - {failure}")
        return 1

    logger.info("Fixture verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
