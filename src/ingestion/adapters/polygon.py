import logging
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.common.config import settings
from src.ingestion.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"

# Columns required in every row returned by fetch()
REQUIRED_COLUMNS = [
    "ticker", "trading_date", "open", "high", "low",
    "close", "volume", "source",
]


class PolygonAdapter(BaseAdapter):
    """
    Adapter for Polygon.io REST API — OHLCV daily bars.

    Rate limits (free tier): 5 requests/minute.
    The caller (CLI/flow) is responsible for sleeping between tickers.
    Retry logic here handles transient HTTP errors only.

    Timestamp note: Polygon daily bar timestamps are midnight UTC of the
    trading date. We use the UTC date directly — do NOT convert to ET
    for daily bars or you'll get an off-by-one error for US equities.
    """

    SOURCE_NAME = "polygon"

    def __init__(self) -> None:
        self.session = requests.Session()
        # Authenticate via header, NOT the ?apiKey= query param. requests embeds the
        # full URL in its exception messages, and those get logged and persisted to
        # pipeline_runs.error_message — a query-param key leaks into both.
        self.session.headers.update(
            {"Authorization": f"Bearer {settings.polygon_api_key}"}
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """
        Make a GET request to the Polygon API.
        Retries up to 3 times on HTTPError with exponential backoff.
        Handles 429 (rate limit) explicitly with a 60-second sleep.
        """
        url = f"{POLYGON_BASE_URL}{path}"
        response = self.session.get(url, params=params or {}, timeout=30)

        if response.status_code == 429:
            logger.warning("Rate limited by Polygon. Sleeping 60 seconds.")
            time.sleep(60)
            response = self.session.get(url, params=params or {}, timeout=30)

        response.raise_for_status()
        return response.json()

    def fetch(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Fetch OHLCV daily bars for a single ticker over a date range.
        Returns an empty DataFrame (not an error) for non-trading days.
        """
        path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
        params = {
            "adjusted": "false",  # We handle adjustment ourselves — raw prices only
            "sort": "asc",
            "limit": 50000,
        }

        data = self._get(path, params)
        results = data.get("results") or []

        if not results:
            logger.info(f"No data returned for {ticker} {start_date}–{end_date} (non-trading day or delisted)")
            return self._empty_dataframe()

        rows = []
        for bar in results:
            # Polygon daily bar timestamp = midnight UTC of trading date.
            # Use UTC date directly. Do NOT convert to ET — that shifts the date back by 1 day.
            trading_date = pd.Timestamp(bar["t"], unit="ms", tz="UTC").date()

            rows.append({
                "ticker": ticker,
                "trading_date": trading_date,
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
                "vwap": bar.get("vw"),       # Optional field
                "trade_count": bar.get("n"), # Optional field
                "source": self.SOURCE_NAME,
                "ingested_at": datetime.now(timezone.utc),
            })

        df = pd.DataFrame(rows)
        logger.info(f"Fetched {len(df)} bars for {ticker} ({start_date}–{end_date})")
        return df

    def validate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Validate schema and type-coerce the raw DataFrame.
        Raises ValueError if required columns are missing.
        """
        if df.empty:
            return df

        missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing:
            raise ValueError(f"PolygonAdapter.validate: missing required columns: {missing}")

        df = df.copy()

        # Ensure correct types — never trust API dtypes
        df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date
        # volume stays floating point on purpose: Polygon reports fractional
        # volume (fractional-share trades aggregated), so casting to an integer
        # here would discard source detail in the raw layer. dbt staging rounds
        # it to a whole share count.
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "trade_count" in df.columns:
            df["trade_count"] = pd.to_numeric(df["trade_count"], errors="coerce").astype("Int64")

        return df

    @staticmethod
    def _empty_dataframe() -> pd.DataFrame:
        """Return a correctly-shaped empty DataFrame when API returns no results."""
        return pd.DataFrame(columns=[
            "ticker", "trading_date", "open", "high", "low", "close",
            "volume", "vwap", "trade_count", "source", "ingested_at",
        ])