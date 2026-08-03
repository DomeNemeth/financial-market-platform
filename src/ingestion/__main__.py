"""
OHLCV ingestion CLI.

    python -m src.ingestion --date 2026-07-29
    python -m src.ingestion --start 2026-06-01 --end 2026-06-30 --tickers AAPL MSFT
    python -m src.ingestion --source yahoo --start 2026-06-01 --end 2026-07-31

Polygon is the primary source and the default; Yahoo is the fallback. The
priority rule that decides which of them reaches the mart is applied in dbt, in
int_prices_merged — NOT here. This CLI lands whatever the chosen vendor said,
per source, exactly as ADR-0008 requires of the raw layer.

Partial-failure policy: collect and continue, then fail the run.
See docs/adr/0011-ingestion-failure-policy.md.
"""

import argparse
import logging
import time
from datetime import date

from src.common.calendar import missing_sessions, session_count
from src.common.logging import configure_logging
from src.common.tls import enable_system_trust_store
from src.ingestion.adapters.base import BaseAdapter
from src.ingestion.adapters.polygon import PolygonAdapter
from src.ingestion.adapters.yahoo import YahooAdapter
from src.ingestion.run_ledger import RunLedger

configure_logging()
# Must run before any outbound HTTPS request. See src/common/tls.py.
enable_system_trust_store()
logger = logging.getLogger(__name__)

# Default ticker list — 10 well-known S&P 500 names
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "JPM", "JNJ", "V",
]

#: Keyed on the adapter's own SOURCE_NAME, so the CLI's --source values and the
#: `source` column in raw.prices can never drift apart.
ADAPTERS: dict[str, type[BaseAdapter]] = {
    PolygonAdapter.SOURCE_NAME: PolygonAdapter,
    YahooAdapter.SOURCE_NAME: YahooAdapter,
}


class PartialIngestionError(Exception):
    """Raised when some tickers succeeded and others failed. See ADR-0011."""


def ingest_ticker(
    adapter: BaseAdapter, ticker: str, start: date, end: date
) -> tuple[int, list[date]]:
    """
    Ingest one ticker over [start, end]. Returns (rows_written, missing_sessions).

    One API call covers the whole range — both vendors' endpoints are
    range-based, so a 30-day backfill costs one request, not 30. That matters on
    Polygon's 5-requests-per-minute tier.
    """
    df = adapter.validate(adapter.fetch(ticker, start, end))

    if df.empty:
        return 0, missing_sessions(set(), start, end)

    # Parquet stays partitioned one file per trading date (ADR-0002), even though
    # the fetch was range-based, so a single date can be re-ingested or diffed
    # without rewriting its neighbours.
    for trading_date, group in df.groupby("trading_date"):
        adapter.write_parquet(group, ticker, trading_date)

    rows = adapter.load_to_postgres(df)
    gaps = missing_sessions(set(df["trading_date"]), start, end)
    return rows, gaps


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest OHLCV data from a vendor into raw Parquet + Postgres"
    )
    parser.add_argument(
        "--source",
        choices=sorted(ADAPTERS),
        default=PolygonAdapter.SOURCE_NAME,
        help="Vendor to ingest from. Polygon is primary (ADR-0006); yahoo is the fallback.",
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
        default=None,
        help="Single trading date, YYYY-MM-DD. Mutually exclusive with --start/--end.",
    )
    parser.add_argument("--start", type=date.fromisoformat, default=None, help="Range start")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="Range end")
    args = parser.parse_args()

    if args.date and (args.start or args.end):
        parser.error("--date cannot be combined with --start/--end")
    if bool(args.start) != bool(args.end):
        parser.error("--start and --end must be given together")

    start = args.start or args.date or date.today()
    end = args.end or args.date or date.today()
    if start > end:
        parser.error(f"--start {start} is after --end {end}")

    expected = session_count(start, end)
    logger.info(
        f"Starting ingestion | source={args.source} | {start} → {end} "
        f"({expected} sessions) | {len(args.tickers)} ticker(s)"
    )

    adapter = ADAPTERS[args.source]()
    failures: dict[str, str] = {}
    all_gaps: dict[str, list[date]] = {}
    total_rows = 0

    with RunLedger(
        flow_name=f"{args.source}_ohlcv",
        metadata={
            "source": args.source,
            "tickers": args.tickers,
            "start": str(start),
            "end": str(end),
            "expected_sessions": expected,
        },
    ) as ledger:
        for i, ticker in enumerate(args.tickers):
            logger.info(f"[{i+1}/{len(args.tickers)}] Ingesting {ticker}")

            try:
                rows, gaps = ingest_ticker(adapter, ticker, start, end)
                total_rows += rows
                if gaps:
                    all_gaps[ticker] = gaps
                    logger.warning(
                        f"  {ticker}: {rows} rows, but {len(gaps)}/{expected} sessions "
                        f"missing (first: {gaps[0]}, last: {gaps[-1]})"
                    )
                else:
                    logger.info(f"  {ticker}: {rows} rows, all {expected} sessions present")

            except Exception as exc:
                # Collect and continue (ADR-0011). Each ticker's load committed
                # in its own transaction, so completed work is durable; the run
                # is still failed below so the ledger never reports SUCCESS for
                # an incomplete batch.
                failures[ticker] = f"{type(exc).__name__}: {exc}"
                logger.error(f"  {ticker}: FAILED — {exc}")

            if i < len(args.tickers) - 1:
                time.sleep(adapter.RATE_LIMIT_SLEEP)

        ledger.record_rows(total_rows)

        if failures:
            summary = "; ".join(f"{t} ({e})" for t, e in failures.items())
            raise PartialIngestionError(
                f"{len(failures)}/{len(args.tickers)} tickers failed: {summary}. "
                f"{total_rows} rows from {len(args.tickers) - len(failures)} "
                "successful tickers were committed."
            )

    logger.info(f"Ingestion complete | total_rows={total_rows}")
    if all_gaps:
        logger.warning(
            f"{len(all_gaps)} ticker(s) have session gaps — this is expected for "
            "securities not listed across the whole range, and a real problem otherwise."
        )


if __name__ == "__main__":
    main()
