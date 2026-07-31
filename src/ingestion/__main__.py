import argparse
import logging
import time
from datetime import date

from src.common.logging import configure_logging
from src.ingestion.adapters.polygon import PolygonAdapter
from src.ingestion.run_ledger import RunLedger

configure_logging()
logger = logging.getLogger(__name__)

# Default ticker list — 10 well-known S&P 500 names
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "JPM", "JNJ", "V",
]

# Polygon free tier: 5 requests per minute = 12 seconds between requests
RATE_LIMIT_SLEEP = 12


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest OHLCV data from Polygon.io into raw Parquet + Postgres"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        help="Space-separated ticker list (default: 10 S&P 500 names)",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=str(date.today()),
        help="Trading date to ingest as YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args()

    logger.info(f"Starting ingestion | date={args.date} | tickers={args.tickers}")

    adapter = PolygonAdapter()

    with RunLedger(
        flow_name="polygon_ohlcv",
        metadata={"tickers": args.tickers, "date": str(args.date)},
    ) as ledger:
        total_rows = 0

        for i, ticker in enumerate(args.tickers):
            logger.info(f"[{i+1}/{len(args.tickers)}] Ingesting {ticker}")

            try:
                df = adapter.fetch(ticker, args.date, args.date)
                df = adapter.validate(df)

                if df.empty:
                    logger.warning(f"  No data for {ticker} on {args.date} — skipping (non-trading day?)")
                    # Still sleep to respect rate limits
                    time.sleep(RATE_LIMIT_SLEEP)
                    continue

                parquet_path = adapter.write_parquet(df, ticker, args.date)
                rows = adapter.load_to_postgres(df)
                total_rows += rows

                logger.info(f"  {ticker}: {rows} rows → Parquet={parquet_path.name}, Postgres=raw.prices")

            except Exception as e:
                logger.error(f"  Failed to ingest {ticker}: {e}")
                raise  # Let RunLedger catch this and record FAILED

            # Rate limit: sleep between every request except the last
            if i < len(args.tickers) - 1:
                time.sleep(RATE_LIMIT_SLEEP)

        ledger.record_rows(total_rows)

    logger.info(f"Ingestion complete | total_rows={total_rows}")


if __name__ == "__main__":
    main()