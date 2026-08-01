import logging
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import ClassVar

import pandas as pd

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
            logger.warning(
                f"write_parquet called with empty DataFrame for {ticker} {partition_date}"
            )
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

    #: Columns written to raw.prices, and the key an upsert conflicts on.
    PRICE_COLUMNS: ClassVar[list[str]] = [
        "ticker", "trading_date", "open", "high", "low", "close",
        "volume", "vwap", "trade_count", "source", "ingested_at",
    ]
    PRICE_CONFLICT_COLUMNS: ClassVar[list[str]] = ["ticker", "trading_date", "source"]

    def load_to_postgres(self, df: pd.DataFrame) -> int:
        """
        Upsert OHLCV rows into raw.prices. Idempotent on
        (ticker, trading_date, source) — re-running must not duplicate rows.

        This is what dbt reads: the Postgres raw schema is the working copy,
        Parquet is the archive.
        """
        from src.common.database import upsert_dataframe

        return upsert_dataframe(
            df,
            target="raw.prices",
            columns=self.PRICE_COLUMNS,
            conflict_columns=self.PRICE_CONFLICT_COLUMNS,
        )