import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """
    Abstract base class for all data source adapters.

    Every adapter follows the same pipeline:
        df = adapter.fetch(ticker, start_date, end_date)
        df = adapter.validate(df)
        adapter.write_parquet(df, ticker, partition_date)   # immutable archive
        adapter.load_to_postgres(df)                        # dbt's working copy

    Subclasses must define SOURCE_NAME and implement fetch() and validate().
    write_parquet() and load_to_postgres() are shared across all adapters.
    """

    SOURCE_NAME: ClassVar[str]  # e.g. "polygon" — must be set on each subclass

    @abstractmethod
    def fetch(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Fetch raw data from the source API for the given ticker and date range.
        - Must raise on HTTP errors (do not return empty on error).
        - Must return an empty DataFrame (not raise) when the API returns no data
          (e.g. weekend, holiday, ticker not found for that period).
        - Include retry logic in subclasses, not here.
        """
        ...

    @abstractmethod
    def validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Validate schema and type-coerce the raw DataFrame returned by fetch().
        - Raise ValueError with a descriptive message if required columns are missing.
        - Coerce numeric types — don't trust API dtypes.
        - Return the cleaned DataFrame.
        """
        ...

    def write_parquet(self, df: pd.DataFrame, ticker: str, partition_date: date) -> Path:
        """
        Write to the partitioned raw landing zone. Idempotent — overwrites on re-run.

        Path pattern: data/raw/prices/{SOURCE_NAME}/{ticker}/{YYYY-MM-DD}.parquet

        This is the immutable archive. Once written, these files should never be 
        mutated — only overwritten on intentional re-ingestion (backfill).
        """
        if df.empty:
            logger.warning(f"write_parquet called with empty DataFrame for {ticker} {partition_date}")
            return Path()

        output_path = (
            Path("data/raw/prices")
            / self.SOURCE_NAME
            / ticker
            / f"{partition_date.isoformat()}.parquet"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(output_path, index=False)
        logger.debug(f"Wrote Parquet: {output_path} ({len(df)} rows)")
        return output_path

    @staticmethod
    def _records(df: pd.DataFrame, columns: list[str]) -> list[dict]:
        """
        Project df onto `columns` and convert to executemany-ready dicts.

        The .astype(object).where(notna) dance is load-bearing: psycopg2 cannot
        adapt pandas sentinels. pd.NA (from Int64 columns like trade_count)
        raises outright, and float('nan') is adapted to a literal Postgres NaN —
        which NUMERIC silently accepts, so a missing vwap would land as NaN
        instead of NULL. Both must become None before they reach the driver.
        """
        projected = df[columns]
        return projected.astype(object).where(pd.notna(projected), None).to_dict("records")

    def upsert(
        self,
        df: pd.DataFrame,
        *,
        target: str,
        columns: list[str],
        conflict_columns: list[str],
        update_columns: list[str],
    ) -> int:
        """
        Idempotently upsert a DataFrame into a raw table via a staging temp table.

        Returns the number of rows inserted or updated.

        The staging table is a genuine session-scoped TEMP table with
        ON COMMIT DROP, not a permanent one. An earlier version wrote to a real
        `raw.prices_tmp`, which had three problems: two concurrent ingestions
        clobbered each other's staging rows, a crash mid-load left a stray table
        sitting in the `raw` schema where dbt introspects sources, and its
        column types were whatever pandas inferred rather than the target's.

        It is built as `CREATE TEMP TABLE ... AS SELECT <cols> FROM <target>
        WITH NO DATA` rather than `LIKE <target>`. LIKE would copy every column
        plus its NOT NULL constraints but *not* its defaults, so the BIGSERIAL
        `id` would arrive as a NOT NULL bigint with no sequence behind it and
        every insert would fail. The WITH NO DATA projection yields exactly the
        columns being written, with the target's exact types and no constraints,
        defaults, or sequence consumption.
        """
        if df.empty:
            return 0

        from src.common.database import engine

        staging = f"_stg_{target.replace('.', '_')}"
        collist = ", ".join(columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        conflict = ", ".join(conflict_columns)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)

        with engine.begin() as conn:
            # Step 1: session-private staging table, dropped when this transaction commits
            conn.execute(text(f"""
                CREATE TEMP TABLE {staging} ON COMMIT DROP AS
                SELECT {collist} FROM {target} WITH NO DATA
            """))

            # Step 2: bulk-insert the batch into staging
            conn.execute(
                text(f"INSERT INTO {staging} ({collist}) VALUES ({placeholders})"),
                self._records(df, columns),
            )

            # Step 3: single set-based upsert into the real table.
            # DISTINCT ON is required, not defensive: Postgres raises
            # "ON CONFLICT DO UPDATE command cannot affect row a second time" if
            # one statement presents two rows with the same conflict key. Vendors
            # do return intra-batch duplicates (overlapping paginated ranges, a
            # restated bar). ctid DESC keeps the physically-last staged row, i.e.
            # last-write-wins in DataFrame order.
            result = conn.execute(text(f"""
                INSERT INTO {target} ({collist})
                SELECT DISTINCT ON ({conflict}) {collist}
                FROM {staging}
                ORDER BY {conflict}, ctid DESC
                ON CONFLICT ({conflict}) DO UPDATE SET {updates}
            """))

            rows_affected = result.rowcount
            logger.info(f"Upserted {rows_affected} rows into {target}")
            return rows_affected

    #: Columns written to raw.prices, and which of them an upsert refreshes.
    PRICE_COLUMNS: ClassVar[list[str]] = [
        "ticker", "trading_date", "open", "high", "low", "close",
        "volume", "vwap", "trade_count", "source", "ingested_at",
    ]
    PRICE_CONFLICT_COLUMNS: ClassVar[list[str]] = ["ticker", "trading_date", "source"]

    def load_to_postgres(self, df: pd.DataFrame) -> int:
        """
        Upsert OHLCV rows into raw.prices. Idempotent on
        (ticker, trading_date, source) — re-running must not duplicate rows.

        This is what dbt reads — the Postgres raw schema is the working copy,
        Parquet is the archive.
        """
        return self.upsert(
            df,
            target="raw.prices",
            columns=self.PRICE_COLUMNS,
            conflict_columns=self.PRICE_CONFLICT_COLUMNS,
            # The conflict key itself is never updated — it is what matched.
            update_columns=[
                c for c in self.PRICE_COLUMNS if c not in self.PRICE_CONFLICT_COLUMNS
            ],
        )