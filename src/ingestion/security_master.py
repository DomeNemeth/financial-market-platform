"""
Security master ingestion: Polygon ticker details, enriched with OpenFIGI.

Implements the identity model in ADR-0004 and ADR-0007. The important idea is
that a ticker is not an identity — it is a time-bounded attribute of one. This
module's real job is minting and maintaining `security_id`, the durable
surrogate every fact table joins on.

Usage:
    python -m src.ingestion.security_master --tickers AAPL MSFT NVDA
"""

import argparse
import logging
import time
from datetime import date, datetime, timezone
from typing import Any

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
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

SOURCE_NAME = "polygon"

SECURITY_MASTER_COLUMNS = [
    "security_id", "ticker", "name",
    "figi", "share_class_figi", "cusip", "isin",
    "exchange", "currency", "security_type", "active",
    "list_date", "delist_date", "source", "ingested_at",
]

# OpenFIGI allows 25 requests/minute unkeyed, 250 with a free API key, and
# batches up to 100 jobs (10 unkeyed) per request.
OPENFIGI_BATCH_SIZE = 10
OPENFIGI_BATCH_SIZE_KEYED = 100
POLYGON_RATE_LIMIT_SLEEP = 12  # free tier: 5 req/min


class SecurityMasterIngestion:
    def __init__(self) -> None:
        self.session = requests.Session()
        # Header auth, never ?apiKey= — requests embeds the full URL in exception
        # messages, which get logged and persisted to pipeline_runs.error_message.
        self.session.headers.update(
            {"Authorization": f"Bearer {settings.polygon_api_key}"}
        )
        self.figi_session = requests.Session()
        if settings.openfigi_api_key:
            self.figi_session.headers.update({"X-OPENFIGI-APIKEY": settings.openfigi_api_key})

    # ------------------------------------------------------------------ fetch

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        response = self.session.get(
            f"{POLYGON_BASE_URL}{path}", params=params or {}, timeout=30
        )
        if response.status_code == 429:
            logger.warning("Rate limited by Polygon. Sleeping 60 seconds.")
            time.sleep(60)
            response = self.session.get(
                f"{POLYGON_BASE_URL}{path}", params=params or {}, timeout=30
            )
        response.raise_for_status()
        return response.json()

    def fetch_ticker_details(self, ticker: str) -> dict[str, Any] | None:
        """
        Fetch Polygon's reference details for one ticker.

        Returns None for a ticker Polygon does not know, rather than raising:
        an unknown symbol in a user-supplied list is a data condition, not a
        failure of the pipeline. Genuine HTTP errors still propagate.
        """
        try:
            data = self._get(f"/v3/reference/tickers/{ticker}")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning(f"  {ticker}: not found in Polygon reference data")
                return None
            raise
        return data.get("results")

    def fetch_figi(self, tickers: list[str]) -> dict[str, dict[str, str]]:
        """
        Resolve tickers to FIGIs via OpenFIGI. Returns {ticker: {figi fields}}.

        Unresolved tickers are simply absent from the result. A FIGI lookup
        failure must not fail the run — it downgrades the security to a
        provisional identity (ADR-0004), which is a recoverable state that a
        later run promotes in place.
        """
        if not tickers:
            return {}

        batch_size = OPENFIGI_BATCH_SIZE_KEYED if settings.openfigi_api_key else OPENFIGI_BATCH_SIZE
        resolved: dict[str, dict[str, str]] = {}

        for start in range(0, len(tickers), batch_size):
            batch = tickers[start : start + batch_size]
            jobs = [{"idType": "TICKER", "idValue": t, "exchCode": "US"} for t in batch]

            try:
                response = self.figi_session.post(OPENFIGI_URL, json=jobs, timeout=30)
                if response.status_code == 429:
                    logger.warning("  OpenFIGI rate limit hit. Sleeping 60s.")
                    time.sleep(60)
                    response = self.figi_session.post(OPENFIGI_URL, json=jobs, timeout=30)
                response.raise_for_status()
                results = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning(f"  OpenFIGI lookup failed for {batch}: {exc}")
                continue

            # OpenFIGI returns results positionally, one entry per job, each
            # either {"data": [...]} or {"warning": "No identifier found."}.
            for ticker, result in zip(batch, results):
                matches = result.get("data") if isinstance(result, dict) else None
                if not matches:
                    logger.info(f"  {ticker}: no FIGI match")
                    continue
                match = matches[0]
                resolved[ticker] = {
                    "figi": match.get("figi"),
                    "share_class_figi": match.get("shareClassFIGI"),
                    "security_type": match.get("securityType2") or match.get("securityType"),
                }

            if start + batch_size < len(tickers):
                time.sleep(2)

        return resolved

    # --------------------------------------------------------------- identity

    @staticmethod
    def resolve_security_id(conn, ticker: str, figi: str | None, source: str) -> int:
        """
        Return the durable security_id for this security, minting one if needed.

        Three cases, in order (ADR-0004):

        1. A FIGI-anchored identity already exists -> reuse it. This is what
           makes a ticker *change* a non-event: FB and META resolve to the same
           FIGI and therefore the same security_id.
        2. A provisional identity exists for this ticker and we now have a FIGI
           -> promote it in place. identity_key and identity_kind are rewritten,
           security_id is not, so every existing foreign key stays valid and no
           history is rewritten.
        3. Nothing exists -> mint a new identity, FIGI-anchored if we have one,
           provisional otherwise.
        """
        provisional_key = f"vendor_ticker:{source}:{ticker}"
        figi_key = f"figi:{figi}" if figi else None

        if figi_key:
            existing = conn.execute(
                text("SELECT security_id FROM raw.security_identity WHERE identity_key = :k"),
                {"k": figi_key},
            ).fetchone()
            if existing:
                # If a provisional row for this ticker ALSO exists, the two rows
                # genuinely describe one security and need merging. Detected here,
                # not fixed: merging means rewriting fact-table foreign keys.
                # Documented as an accepted gap in ADR-0004.
                stale = conn.execute(
                    text("""
                        SELECT security_id FROM raw.security_identity
                        WHERE identity_key = :k AND identity_kind = 'vendor_ticker'
                    """),
                    {"k": provisional_key},
                ).fetchone()
                if stale and stale[0] != existing[0]:
                    logger.warning(
                        f"  {ticker}: provisional security_id={stale[0]} and FIGI-anchored "
                        f"security_id={existing[0]} both exist and need a manual merge "
                        f"(ADR-0004, known gap). Using {existing[0]}."
                    )
                return int(existing[0])

            promoted = conn.execute(
                text("""
                    UPDATE raw.security_identity
                    SET identity_key = :new_key,
                        identity_kind = 'figi',
                        resolved_at = NOW()
                    WHERE identity_key = :old_key AND identity_kind = 'vendor_ticker'
                    RETURNING security_id
                """),
                {"new_key": figi_key, "old_key": provisional_key},
            ).fetchone()
            if promoted:
                logger.info(f"  {ticker}: promoted provisional identity -> {figi_key}")
                return int(promoted[0])

        key = figi_key or provisional_key
        kind = "figi" if figi_key else "vendor_ticker"

        # ON CONFLICT DO NOTHING then SELECT, rather than DO UPDATE RETURNING:
        # a no-op update would still bump the row and, more importantly, this
        # keeps first_seen_at meaning what it says.
        conn.execute(
            text("""
                INSERT INTO raw.security_identity (identity_key, identity_kind, resolved_at)
                VALUES (:key, :kind, CASE WHEN :kind = 'figi' THEN NOW() END)
                ON CONFLICT (identity_key) DO NOTHING
            """),
            {"key": key, "kind": kind},
        )
        row = conn.execute(
            text("SELECT security_id FROM raw.security_identity WHERE identity_key = :k"),
            {"k": key},
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ shape

    @staticmethod
    def to_row(details: dict[str, Any], figi_data: dict[str, str], security_id: int) -> dict:
        """Map a Polygon ticker-details payload onto raw.security_master columns."""
        delisted = details.get("delisted_utc")
        return {
            "security_id": security_id,
            "ticker": details["ticker"],
            "name": details.get("name"),
            "figi": figi_data.get("figi") or details.get("composite_figi"),
            "share_class_figi": (
                figi_data.get("share_class_figi") or details.get("share_class_figi")
            ),
            # CUSIP/ISIN are licensed identifiers. Polygon returns them only on
            # paid plans, and we must not redistribute them. NULL is the honest
            # value and is never backfilled by inference. See ADR-0007.
            "cusip": None,
            "isin": None,
            "exchange": details.get("primary_exchange"),
            "currency": (details.get("currency_name") or "")[:3].upper() or None,
            "security_type": details.get("type") or figi_data.get("security_type"),
            "active": details.get("active"),
            "list_date": (
                date.fromisoformat(details["list_date"]) if details.get("list_date") else None
            ),
            "delist_date": date.fromisoformat(delisted[:10]) if delisted else None,
            "source": SOURCE_NAME,
            "ingested_at": datetime.now(timezone.utc),
        }

    # -------------------------------------------------------------------- run

    def ingest(self, tickers: list[str]) -> int:
        """Fetch, resolve identity for, and upsert the given tickers. Returns rows written."""
        logger.info(f"Fetching Polygon ticker details for {len(tickers)} ticker(s)")

        details_by_ticker: dict[str, dict] = {}
        for i, ticker in enumerate(tickers):
            details = self.fetch_ticker_details(ticker)
            if details:
                details_by_ticker[ticker] = details
                logger.info(f"  {ticker}: {details.get('name')}")
            if i < len(tickers) - 1:
                time.sleep(POLYGON_RATE_LIMIT_SLEEP)

        if not details_by_ticker:
            logger.warning("No ticker details resolved — nothing to write")
            return 0

        logger.info("Resolving FIGIs via OpenFIGI")
        figis = self.fetch_figi(list(details_by_ticker))

        rows = []
        with engine.begin() as conn:
            for ticker, details in details_by_ticker.items():
                figi_data = figis.get(ticker, {})
                figi = figi_data.get("figi") or details.get("composite_figi")
                security_id = self.resolve_security_id(conn, ticker, figi, SOURCE_NAME)
                rows.append(self.to_row(details, figi_data, security_id))

            df = pd.DataFrame(rows)
            written = upsert_dataframe(
                df,
                target="raw.security_master",
                columns=SECURITY_MASTER_COLUMNS,
                conflict_columns=["security_id", "source"],
                conn=conn,
            )

        unresolved = sum(1 for r in rows if not r["figi"])
        if unresolved:
            logger.warning(
                f"{unresolved}/{len(rows)} securities have no FIGI and hold provisional "
                "identities. Re-run to promote them once OpenFIGI resolves."
            )
        return written


def main() -> None:
    configure_logging()
    enable_system_trust_store()  # must precede any outbound HTTPS. See src/common/tls.py

    parser = argparse.ArgumentParser(description="Ingest security master reference data")
    parser.add_argument("--tickers", nargs="+", required=True, help="Tickers to ingest")
    args = parser.parse_args()

    from src.ingestion.run_ledger import RunLedger

    with RunLedger(
        flow_name="security_master", metadata={"tickers": args.tickers}
    ) as ledger:
        rows = SecurityMasterIngestion().ingest(args.tickers)
        ledger.record_rows(rows)

    logger.info(f"Security master ingestion complete | rows={rows}")


if __name__ == "__main__":
    main()
