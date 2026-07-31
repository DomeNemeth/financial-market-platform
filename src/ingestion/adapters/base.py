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

    def load_to_postgres(self, df: pd.DataFrame) -> int:
        """
        Upsert DataFrame into raw.prices in Postgres.
        Uses a staging temp table + INSERT ... ON CONFLICT for idempotency.
        Returns the number of rows affected.

        This is what dbt reads — the Postgres raw schema is the working copy,
        Parquet is the archive.
        """
        if df.empty:
            return 0

        from src.common.database import engine

        with engine.begin() as conn:
            # Step 1: Write to a temp table (created fresh each time)
            df.to_sql(
                "prices_tmp",
                conn,
                schema="raw",
                if_exists="replace",  # Drop and recreate on every load
                index=False,
                method="multi",
                chunksize=1000,
            )

            # Step 2: Upsert from temp into the real table
            result = conn.execute(text("""
                INSERT INTO raw.prices (
                    ticker, trading_date, open, high, low, close,
                    volume, vwap, trade_count, source, ingested_at
                )
                SELECT
                    ticker, trading_date, open, high, low, close,
                    volume, vwap, trade_count, source, ingested_at
                FROM raw.prices_tmp
                ON CONFLICT (ticker, trading_date, source)
                DO UPDATE SET
                    open        = EXCLUDED.open,
                    high        = EXCLUDED.high,
                    low         = EXCLUDED.low,
                    close       = EXCLUDED.close,
                    volume      = EXCLUDED.volume,
                    vwap        = EXCLUDED.vwap,
                    trade_count = EXCLUDED.trade_count,
                    ingested_at = EXCLUDED.ingested_at
            """))

            rows_affected = result.rowcount
            logger.info(f"Upserted {rows_affected} rows into raw.prices (source={df['source'].iloc[0] if len(df) else 'unknown'})")
            return rows_affected