"""
Export the CI warehouse fixtures from a populated local warehouse.

The inverse of `scripts/load_ci_fixtures.py`, and the only supported way to
refresh `tests/fixtures/ci/`. Run it against a database that has real ingested
data in it — not against CI's own database, which would produce a fixture that
is a copy of itself and would let any lost fact stay lost forever.

    .venv\\Scripts\\python.exe -m scripts.export_ci_fixtures

It re-runs the loader's `verify()` invariants against the *live* database before
writing anything. That ordering is deliberate: a warehouse that has drifted —
KLAC's split re-ingested under a different source, the Yahoo overlap bars
trimmed, DGS10's holiday sentinel repaired into a zero — would otherwise be
exported into a fixture that loads cleanly and silently disarms the tests that
depend on those facts. Refusing to export is the loud failure; a green CI run
against a hollowed-out fixture is the quiet one.

Column lists come from `load_ci_fixtures.TABLES`, which in turn imports them
from the ingestion modules that own them. There is no third restatement of a
schema here.
"""

from __future__ import annotations

import logging
import sys

import pandas as pd
from sqlalchemy import text

from scripts.load_ci_fixtures import (
    FIXTURE_DIR,
    MACRO_CUTOFF,
    TABLES,
    FixtureTable,
    verify,
)
from src.common.database import engine

logger = logging.getLogger(__name__)

#: Deterministic row order per table, so a re-export produces a diff that shows
#: what actually changed in the data rather than whatever order Postgres
#: happened to return. Without this, every refresh is an unreadable whole-file
#: diff and nobody reviews it.
ORDER_BY: dict[str, str] = {
    "security_identity": "security_id",
    "security_master": "security_id, source",
    "corporate_actions": "ticker, action_type, ex_date, source",
    "prices": "source, ticker, trading_date",
    "macro_series": "series_id, source",
    "macro_observations": "series_id, observation_date, source",
}

#: The one table that is subset rather than copied whole. ADR-0013 records the
#: three properties this cutoff was verified to preserve.
WHERE: dict[str, str] = {
    "macro_observations": f"observation_date >= DATE '{MACRO_CUTOFF}'",
}


def export(table: FixtureTable) -> int:
    """
    Dump one table, letting Postgres do every string conversion.

    EVERY COLUMN IS CAST `::text` IN SQL. This is not decoration, and the first
    version of this script did not do it. Without the cast, pandas infers a
    dtype per column, and a nullable BIGINT — `trade_count`, which is NULL on
    every Yahoo bar by design — is inferred as float64 because NaN is the only
    null float64 has. `to_csv` then writes `904768.0`, and the loader hands that
    string to a BIGINT column, which Postgres rejects outright:

        invalid input syntax for type integer: "904768.0"

    Loud, and therefore survivable. The same inference is not survivable one
    column to the left: `volume` and every price are NUMERIC, so a float64
    round-trip is silently *accepted*, and ADR-0003's whole claim is that money
    is Decimal and never float. A fixture that had quietly lost digits would
    have loaded cleanly and then failed the split reconciliation at 1e-9 with
    nothing pointing at the cause.

    Casting in SQL means Postgres emits its own canonical text for each type and
    pandas only ever sees `str` and `None` — the same representation
    `read_fixture()` reads back. The value never becomes a Python number in
    either direction.
    """
    # Fixed identifiers from FixtureTable, never user input.
    columns = ", ".join(f"{c}::text AS {c}" for c in table.columns)
    where = f"WHERE {WHERE[table.name]}" if table.name in WHERE else ""
    order = ORDER_BY[table.name]

    with engine.connect() as conn:
        df = pd.read_sql(
            text(f"SELECT {columns} FROM {table.target} {where} ORDER BY {order}"),
            conn,
        )

    table.path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(table.path, index=False, lineterminator="\n")
    return len(df)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    logger.info("Checking the source warehouse still carries every fixture invariant")
    failures = verify()
    if failures:
        logger.error("Source warehouse FAILED verification — refusing to export:")
        for failure in failures:
            logger.error(f"  - {failure}")
        logger.error(
            "\nThe local warehouse has drifted from what the test suites assume. "
            "Exporting now would bake that drift into CI. Re-ingest the missing "
            "data before refreshing the fixtures."
        )
        return 1

    logger.info(f"Writing fixtures to {FIXTURE_DIR}")
    total = 0
    for table in TABLES:
        rows = export(table)
        total += rows
        size_kb = table.path.stat().st_size / 1024
        logger.info(f"{table.path.name:<28} {rows:>6} rows  {size_kb:>7.1f} KB")

    logger.info(f"Exported {total} rows across {len(TABLES)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
