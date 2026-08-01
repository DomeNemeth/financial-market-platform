"""
Corporate action ingestion: splits and cash dividends from Polygon.

These rows are the sole input to the adjustment factors in ADR-0003. A missing
split does not produce an error — it produces a price series with a 90% single-day
"return" in it, so completeness matters more here than almost anywhere else in
the platform.

Usage:
    python -m src.ingestion.corporate_actions --tickers NVDA AAPL
    python -m src.ingestion.corporate_actions --tickers NVDA --since 2024-01-01
"""

import argparse
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Iterator

import pandas as pd
import requests
from sqlalchemy import text
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.common.config import settings
from src.common.database import engine, upsert_dataframe
from src.common.logging import configure_logging
from src.common.tls import enable_system_trust_store

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"
SOURCE_NAME = "polygon"
RATE_LIMIT_SLEEP = 12  # free tier: 5 requests/minute

CORPORATE_ACTION_COLUMNS = [
    "security_id", "ticker", "action_type", "ex_date",
    "split_to", "split_from",
    "cash_amount", "currency", "dividend_type",
    "declaration_date", "record_date", "pay_date",
    "source", "ingested_at",
]


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


class CorporateActionsIngestion:
    def __init__(self) -> None:
        self.session = requests.Session()
        # Header auth, never ?apiKey= — see the note in the Polygon price adapter.
        self.session.headers.update(
            {"Authorization": f"Bearer {settings.polygon_api_key}"}
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _get(self, url: str, params: dict | None = None) -> dict[str, Any]:
        response = self.session.get(url, params=params or {}, timeout=30)
        if response.status_code == 429:
            logger.warning("Rate limited by Polygon. Sleeping 60 seconds.")
            time.sleep(60)
            response = self.session.get(url, params=params or {}, timeout=30)
        response.raise_for_status()
        return response.json()

    def _paginate(self, path: str, params: dict) -> Iterator[dict[str, Any]]:
        """
        Yield every result across Polygon's cursor pagination.

        Following next_url is not optional: it defaults to 10 results per page,
        and a ticker with a long dividend history silently truncates without it —
        which would drop the oldest actions, the ones with the largest cumulative
        effect on adjusted prices.
        """
        url = f"{POLYGON_BASE_URL}{path}"
        page_params: dict | None = {**params, "limit": 1000}

        while url:
            data = self._get(url, page_params)
            yield from data.get("results") or []

            url = data.get("next_url")
            # next_url already encodes the query string; re-sending params would
            # duplicate them. Auth stays in the session header either way.
            page_params = None
            if url:
                time.sleep(RATE_LIMIT_SLEEP)

    def fetch_splits(self, ticker: str, since: date | None = None) -> list[dict]:
        params: dict[str, Any] = {"ticker": ticker, "order": "asc", "sort": "execution_date"}
        if since:
            params["execution_date.gte"] = since.isoformat()
        results = list(self._paginate("/v3/reference/splits", params))
        logger.info(f"  {ticker}: {len(results)} split(s)")
        return results

    def fetch_dividends(self, ticker: str, since: date | None = None) -> list[dict]:
        params: dict[str, Any] = {"ticker": ticker, "order": "asc", "sort": "ex_dividend_date"}
        if since:
            params["ex_dividend_date.gte"] = since.isoformat()
        results = list(self._paginate("/v3/reference/dividends", params))
        logger.info(f"  {ticker}: {len(results)} dividend(s)")
        return results

    @staticmethod
    def split_to_row(raw: dict, security_id: int) -> dict | None:
        """
        Map a Polygon split payload onto raw.corporate_actions.

        Polygon reports splits as split_from -> split_to (NVDA 2024-06-10 was
        from=1, to=10). The ratio used by the adjustment maths is to/from, but
        both sides are stored verbatim so the vendor's own numbers survive
        (ADR-0002).
        """
        ex_date = _as_date(raw.get("execution_date"))
        split_to, split_from = raw.get("split_to"), raw.get("split_from")

        # The DB CHECK constraint would reject these anyway; catching them here
        # names the offending record instead of failing the whole batch opaquely.
        if not ex_date or not split_to or not split_from:
            logger.warning(f"  Skipping malformed split (incomplete payload): {raw}")
            return None

        return {
            "security_id": security_id,
            "ticker": raw["ticker"],
            "action_type": "split",
            "ex_date": ex_date,
            "split_to": split_to,
            "split_from": split_from,
            "cash_amount": None,
            "currency": None,
            "dividend_type": None,
            "declaration_date": None,
            "record_date": None,
            "pay_date": None,
            "source": SOURCE_NAME,
            "ingested_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def dividend_to_row(raw: dict, security_id: int) -> dict | None:
        """Map a Polygon dividend payload onto raw.corporate_actions."""
        ex_date = _as_date(raw.get("ex_dividend_date"))
        amount = raw.get("cash_amount")

        if not ex_date or amount is None or amount <= 0:
            logger.warning(f"  Skipping malformed dividend (incomplete payload): {raw}")
            return None

        return {
            "security_id": security_id,
            "ticker": raw["ticker"],
            "action_type": "dividend",
            "ex_date": ex_date,
            "split_to": None,
            "split_from": None,
            "cash_amount": amount,
            "currency": (raw.get("currency") or "USD")[:3].upper(),
            "dividend_type": raw.get("dividend_type"),
            "declaration_date": _as_date(raw.get("declaration_date")),
            "record_date": _as_date(raw.get("record_date")),
            "pay_date": _as_date(raw.get("pay_date")),
            "source": SOURCE_NAME,
            "ingested_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def lookup_security_id(conn, ticker: str) -> int:
        """
        Find the security_id for a ticker via the security master.

        Corporate actions deliberately do NOT mint identities. If a ticker has no
        security master entry, that is an ordering error the operator needs to see
        — minting a second, unlinked identity here would split one security's
        history across two surrogate keys and silently corrupt its adjustment
        factors.
        """
        row = conn.execute(
            text("""
                SELECT security_id FROM raw.security_master
                WHERE ticker = :ticker
                ORDER BY ingested_at DESC
                LIMIT 1
            """),
            {"ticker": ticker},
        ).fetchone()

        if not row:
            raise LookupError(
                f"No security master entry for {ticker!r}. Run "
                f"`python -m src.ingestion.security_master --tickers {ticker}` first."
            )
        return int(row[0])

    def ingest(self, tickers: list[str], since: date | None = None) -> int:
        rows: list[dict] = []

        with engine.begin() as conn:
            security_ids = {t: self.lookup_security_id(conn, t) for t in tickers}

        for i, ticker in enumerate(tickers):
            security_id = security_ids[ticker]
            logger.info(f"[{i+1}/{len(tickers)}] {ticker} (security_id={security_id})")

            for raw in self.fetch_splits(ticker, since):
                row = self.split_to_row(raw, security_id)
                if row:
                    rows.append(row)
            time.sleep(RATE_LIMIT_SLEEP)

            for raw in self.fetch_dividends(ticker, since):
                row = self.dividend_to_row(raw, security_id)
                if row:
                    rows.append(row)
            if i < len(tickers) - 1:
                time.sleep(RATE_LIMIT_SLEEP)

        if not rows:
            logger.warning("No corporate actions found for the requested tickers/range")
            return 0

        return upsert_dataframe(
            pd.DataFrame(rows),
            target="raw.corporate_actions",
            columns=CORPORATE_ACTION_COLUMNS,
            conflict_columns=["security_id", "action_type", "ex_date", "source"],
        )


def main() -> None:
    configure_logging()
    enable_system_trust_store()

    parser = argparse.ArgumentParser(description="Ingest splits and dividends from Polygon")
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument(
        "--since",
        type=date.fromisoformat,
        default=None,
        help="Only actions with ex_date >= this (YYYY-MM-DD). Default: full history.",
    )
    args = parser.parse_args()

    from src.ingestion.run_ledger import RunLedger

    with RunLedger(
        flow_name="corporate_actions",
        metadata={"tickers": args.tickers, "since": str(args.since)},
    ) as ledger:
        rows = CorporateActionsIngestion().ingest(args.tickers, args.since)
        ledger.record_rows(rows)

    logger.info(f"Corporate action ingestion complete | rows={rows}")


if __name__ == "__main__":
    main()
