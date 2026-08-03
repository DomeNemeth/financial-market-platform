import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.ingestion.adapters.base import BaseAdapter

logger = logging.getLogger(__name__)

YAHOO_BASE_URL = "https://query1.finance.yahoo.com"

# Columns required in every row returned by fetch()
REQUIRED_COLUMNS = [
    "ticker", "trading_date", "open", "high", "low",
    "close", "volume", "source",
]


class YahooAdapter(BaseAdapter):
    """
    Adapter for Yahoo Finance's chart endpoint — OHLCV daily bars.

    The FALLBACK source. Polygon is primary and wins wherever it has a bar; see
    ADR-0006 for the priority rule and the three grounds it rests on.

    ------------------------------------------------------------------------
    THE BASIS PROBLEM — read this before using these bars for anything.

    Polygon is fetched with adjusted=false, so its bars are the unadjusted
    prints. Yahoo HAS NO SUCH FLAG. This endpoint's `quote` arrays are already
    back-adjusted for splits as of the moment of the fetch, and there is no
    parameter that turns that off.

    Measured on KLAC's 10-for-1 split of 2026-06-12: this adapter's 2026-06-11
    close is 241.164001 where Polygon's is 2411.64, exactly a factor of 10, and
    the volumes are inverted by the same factor. From the ex-date onward the two
    agree.

    So these bars are NOT stored in the same units as Polygon's, and they are not
    made so here. Raw stays raw (ADR-0008) — the de-adjustment back onto the raw
    basis happens in dbt, in int_prices_merged, using int_splits__cumulative.
    Doing it in the adapter would make raw.prices unfaithful to the vendor and
    unauditable against the Parquet archive (ADR-0002), and would silently bake
    the platform's split history into a column that is supposed to be Yahoo's
    own record.

    ------------------------------------------------------------------------
    Rate limits: unpublished. Yahoo is tolerant at this volume but will
    eventually throttle; the caller sleeps between tickers, as with Polygon.

    Timestamp note: unlike Polygon's midnight-UTC daily bars, Yahoo stamps a
    daily bar at the session OPEN in exchange-local time (13:30Z under EDT,
    14:30Z under EST). The UTC date happens to coincide with the session date
    for US equities, but that is a coincidence of the US market opening after
    00:00Z — it would not hold for an exchange east of Greenwich. The trading
    date is therefore taken by converting to the exchange timezone the response
    itself declares, never from the UTC date.
    """

    SOURCE_NAME = "yahoo"

    #: Yahoo publishes no rate limit. 2s between tickers is politeness, not a
    #: documented requirement — contrast Polygon's hard 5-requests-per-minute.
    RATE_LIMIT_SLEEP = 2

    def __init__(self) -> None:
        self.session = requests.Session()
        # Yahoo rejects requests/urllib default User-Agents with a 429 that has
        # nothing to do with rate limiting. No credential is involved — this
        # endpoint is unauthenticated, which is itself part of why it is the
        # fallback and not the primary.
        self.session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; financial-market-platform/0.1)"}
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        """
        GET against the Yahoo chart API, retrying transient HTTP errors.

        A 404 is NOT retried and NOT raised: Yahoo encodes "no data for this
        symbol/range" as a 404 carrying a JSON error body, where Polygon encodes
        the same condition as a 200 with an empty results list. BaseAdapter's
        contract says that condition returns an empty DataFrame rather than
        raising, so the status is translated here and the two adapters behave
        the same way for the same input. Every other error status still raises.
        """
        url = f"{YAHOO_BASE_URL}{path}"
        response = self.session.get(url, params=params or {}, timeout=30)

        if response.status_code == 404:
            description = ""
            try:
                error = (response.json().get("chart") or {}).get("error") or {}
                description = error.get("description", "")
            except ValueError:
                pass
            logger.info(f"Yahoo returned 404 for {path} — {description or 'no data'}")
            return {}

        response.raise_for_status()
        return response.json()

    def fetch(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        """
        Fetch OHLCV daily bars for a single ticker over [start_date, end_date].
        Returns an empty DataFrame (not an error) when there is no data.
        """
        # period2 is exclusive of the instant, not of the day. A bar stamped
        # 13:30Z on end_date is only included if the bound is strictly after it,
        # so the range is widened by a day rather than by an hour — an hour
        # would be correct under EDT and wrong under EST.
        period1 = int(datetime.combine(start_date, time(), timezone.utc).timestamp())
        period2 = int(
            datetime.combine(end_date + timedelta(days=1), time(), timezone.utc).timestamp()
        )

        data = self._get(
            f"/v8/finance/chart/{ticker}",
            {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
            },
        )

        results = ((data.get("chart") or {}).get("result")) or []
        if not results:
            logger.info(
                f"No data returned for {ticker} {start_date}–{end_date} "
                "(non-trading day, unknown symbol, or delisted)"
            )
            return self._empty_dataframe()

        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]

        # The exchange's own timezone, as declared by the response. See the class
        # docstring: the UTC date is not the session date in general.
        exchange_tz = ZoneInfo(result["meta"]["exchangeTimezoneName"])

        ingested_at = datetime.now(timezone.utc)
        rows = []
        for i, ts in enumerate(timestamps):
            close = _at(quote.get("close"), i)

            # Yahoo pads its arrays with nulls for sessions it has no print for
            # (halts, and the in-progress session on an intraday fetch). A row
            # with no close is not a bar; it would fail the staging sanity
            # filter anyway, but landing it would put a NULL close in raw and
            # make the row count disagree with the session count for no reason.
            if close is None:
                continue

            rows.append({
                "ticker": ticker,
                "trading_date": datetime.fromtimestamp(ts, exchange_tz).date(),
                "open": _at(quote.get("open"), i),
                "high": _at(quote.get("high"), i),
                "low": _at(quote.get("low"), i),
                "close": close,
                "volume": _at(quote.get("volume"), i),

                # NOT fabricated. Yahoo's chart endpoint supplies neither a
                # volume-weighted average price nor a trade count, and both
                # could be faked convincingly — vwap as (h+l+c)/3, trade_count
                # as 0 — with nothing downstream able to tell. ADR-0006 rejects
                # that on the same grounds ADR-0007 rejects fabricated CUSIPs: a
                # plausible wrong number is worse than a NULL, because NULL is a
                # value a consumer can handle correctly.
                "vwap": None,
                "trade_count": None,

                "source": self.SOURCE_NAME,
                "ingested_at": ingested_at,
            })

        if not rows:
            logger.info(f"All {len(timestamps)} Yahoo bars for {ticker} had a null close")
            return self._empty_dataframe()

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
            raise ValueError(f"YahooAdapter.validate: missing required columns: {missing}")

        df = df.copy()
        df["trading_date"] = pd.to_datetime(df["trading_date"]).dt.date

        # Same coercion as Polygon, including volume staying floating point.
        # Yahoo reports whole-share volume, so nothing is lost — but the column
        # is NUMERIC(20,6) because Polygon's is fractional, and coercing to an
        # integer here would make the two adapters disagree about the dtype of a
        # shared column for no benefit.
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "trade_count" in df.columns:
            df["trade_count"] = pd.to_numeric(df["trade_count"], errors="coerce").astype("Int64")

        return df

    @staticmethod
    def _empty_dataframe() -> pd.DataFrame:
        """Return a correctly-shaped empty DataFrame when the API returns no results."""
        return pd.DataFrame(columns=[
            "ticker", "trading_date", "open", "high", "low", "close",
            "volume", "vwap", "trade_count", "source", "ingested_at",
        ])


def _at(values: list | None, i: int) -> Any:
    """
    Index into one of Yahoo's parallel arrays, tolerating a short or absent one.

    The arrays are documented to be the same length as `timestamp` and usually
    are. When they are not, the alternative to this guard is an IndexError
    halfway through a ticker, which under ADR-0011 fails that ticker and
    continues — losing bars that were fetched successfully for a reason that is
    not a data problem.
    """
    if not values or i >= len(values):
        return None
    return values[i]
