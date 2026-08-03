"""
FRED macroeconomic series ingestion.

    python -m src.ingestion.fred
    python -m src.ingestion.fred --series UNRATE DGS10 --start 2020-01-01

Fetches series metadata and observations from the St. Louis Fed's FRED API into
raw.macro_series and raw.macro_observations.

Deliberately NOT a BaseAdapter subclass. That contract is shaped around OHLCV
price bars — fetch(ticker, start, end), a Parquet archive partitioned per ticker
per trading date, load_to_postgres() writing raw.prices with a fixed column list.
None of it fits a macro series: there is no ticker, no trading date, a quarterly
series has four observations a year rather than one per session, and two
different tables have to be written per series. Forcing it into the adapter
hierarchy would mean overriding every method and inheriting nothing, which is
worse than not inheriting.

What IS shared is the part that carries a guarantee: every write goes through
upsert_dataframe(), so the idempotency property holds here exactly as it does for
prices, and the run is wrapped in RunLedger.

Partial-failure policy: collect and continue, then fail the run (ADR-0011), the
same as price ingestion.
"""

import argparse
import logging
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
from src.common.database import upsert_dataframe
from src.common.logging import configure_logging
from src.common.tls import enable_system_trust_store
from src.ingestion.run_ledger import RunLedger

configure_logging()
# Must run before any outbound HTTPS request. See src/common/tls.py.
enable_system_trust_store()
logger = logging.getLogger(__name__)

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

SOURCE_NAME = "fred"

#: FRED's missing-value sentinel. The API returns this STRING — not null, not an
#: omitted row — for a period with no observation. See _to_value().
MISSING_VALUE_SENTINEL = "."

#: A realtime window wide enough to cover FRED's entire archive, so that
#: output_type=4 reports each observation's genuine first-release date rather
#: than clipping to a recent window. These are FRED's own documented bounds.
EARLIEST_REALTIME = "1776-07-04"
LATEST_REALTIME = "9999-12-31"

#: Ten series spanning the frequencies and unit families that make macro data
#: awkward, chosen so the downstream models are exercised rather than merely
#: populated: quarterly/monthly/daily, levels/rates/indices, and one spread that
#: can go negative.
DEFAULT_SERIES = [
    "GDP",        # Quarterly, billions of dollars — the coarsest grain
    "UNRATE",     # Monthly, percent
    "CPIAUCSL",   # Monthly, index 1982-84=100 — an arbitrary base
    "FEDFUNDS",   # Monthly, percent — the policy rate
    "DGS10",      # Daily, percent — carries the '.' sentinel on market holidays
    "DGS2",       # Daily, percent
    "T10Y2Y",     # Daily, percent — a SPREAD, and legitimately negative
    "PAYEMS",     # Monthly, thousands of persons — a level, not a rate
    "INDPRO",     # Monthly, index
    "UMCSENT",    # Monthly, index — survey-based, revised heavily
]

#: Columns written to each table, and the keys their upserts conflict on.
SERIES_COLUMNS = [
    "series_id", "source", "title", "frequency", "frequency_short",
    "units", "units_short", "seasonal_adjustment", "seasonal_adjustment_short",
    "observation_start", "observation_end", "last_updated", "notes",
    "ingested_at",
]
SERIES_CONFLICT_COLUMNS = ["series_id", "source"]

OBSERVATION_COLUMNS = [
    "series_id", "observation_date", "source", "value",
    "first_published_date", "vintage_date", "ingested_at",
]
OBSERVATION_CONFLICT_COLUMNS = ["series_id", "observation_date", "source"]


class PartialIngestionError(Exception):
    """Raised when some series succeeded and others failed. See ADR-0011."""


class FredClient:
    """Thin HTTP client for the FRED API."""

    def __init__(self) -> None:
        if not settings.fred_api_key:
            # Fail here, loudly, rather than on the first request. FRED rejects
            # an unauthenticated call with a 400 whose body talks about the
            # api_key parameter, which reads like a bug in the query rather than
            # a missing credential.
            raise RuntimeError(
                "FRED_API_KEY is not set. Get a free key at "
                "https://fredaccount.stlouisfed.org/apikeys and add it to .env."
            )
        self.session = requests.Session()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(requests.HTTPError),
        reraise=True,
    )
    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        GET against the FRED API, retrying transient HTTP errors.

        The api_key goes in the query string because FRED accepts it nowhere
        else — it supports no header or bearer authentication. That conflicts
        with the project's "API keys go in headers, never query params" rule,
        whose reason is that `requests` embeds the full URL in exception
        messages and those get persisted to pipeline_runs.error_message. The
        rule cannot be honoured here, so the leak is closed at the other end
        instead: _redact() scrubs the key from every exception before it can
        reach a log or the ledger.
        """
        response = self.session.get(
            f"{FRED_BASE_URL}{path}",
            params={**params, "api_key": settings.fred_api_key, "file_type": "json"},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise requests.HTTPError(_redact(str(exc))) from None
        return response.json()


def _redact(message: str) -> str:
    """Remove the API key from a message bound for a log or the run ledger."""
    if settings.fred_api_key:
        return message.replace(settings.fred_api_key, "***REDACTED***")
    return message


def _to_value(raw: str | None) -> float | None:
    """
    Convert FRED's observation value to a number, or None when it is missing.

    FRED signals a missing observation with the STRING ".", not with null and
    not by omitting the row. DGS10 carries one on 2026-07-03, the observed
    Independence Day holiday.

    Handled explicitly rather than by letting float() raise or letting
    pd.to_numeric(errors='coerce') swallow it, because the two failure modes are
    opposite and both are bad: an unguarded float(".") aborts a whole series on
    a routine market holiday, and a blanket coerce would turn a genuinely
    malformed value into a silent NULL indistinguishable from a real "." — so a
    vendor sending "4,52" or "N/A" would vanish rather than be noticed.

    Anything that is neither the sentinel nor parseable raises, which under
    ADR-0011 fails that series and leaves the rest of the run intact.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped == MISSING_VALUE_SENTINEL or stripped == "":
        return None
    return float(stripped)


def _to_date(raw: str | None) -> date | None:
    """
    Parse a FRED date, treating its open-ended sentinel as unbounded.

    FRED uses 9999-12-31 for "still current" in realtime_end. Postgres DATE
    accepts it, but storing it would mean every downstream comparison had to
    know the magic value. None is the honest representation of unbounded and is
    what the rest of this schema already uses (delist_date, valid_to).
    """
    if not raw or raw == LATEST_REALTIME:
        return None
    return date.fromisoformat(raw)


def fetch_series_metadata(client: FredClient, series_id: str) -> dict[str, Any]:
    """Fetch one series' descriptive metadata."""
    payload = client.get("/series", {"series_id": series_id})
    entries = payload.get("seriess") or []
    if not entries:
        raise ValueError(f"FRED returned no metadata for series {series_id!r}")
    return entries[0]


def fetch_observations(
    client: FredClient, series_id: str, start: date | None = None
) -> list[dict[str, Any]]:
    """
    Fetch the LATEST vintage of every observation for a series.

    This is the current best estimate of each period — the number a chart should
    show. It carries no usable publication date: FRED stamps realtime_start on
    every row with the date of the fetch, which is true and useless for a
    point-in-time join. fetch_first_release_dates() supplies that separately.
    """
    params: dict[str, Any] = {"series_id": series_id}
    if start:
        params["observation_start"] = start.isoformat()
    return client.get("/series/observations", params).get("observations") or []


def fetch_first_release_dates(
    client: FredClient, series_id: str, start: date | None = None
) -> dict[date, date]:
    """
    Map each observation date to the date its FIRST estimate was published.

    output_type=4 is "initial release only", and it is the only way FRED will
    report a genuine publication date. The realtime window has to be widened to
    FRED's full archive bounds as well — without that the response is clipped to
    today and every realtime_start comes back as the fetch date again.

    Measured on UNRATE: January 2026's observation is DATED 2026-01-01 and was
    first published on 2026-02-11, a forty-one day lag. That lag is the entire
    reason this function exists. A macro join that ignores it leaks a number
    nobody could have known, and the resulting backtest is wrong in the
    flattering direction.

    Returns an empty mapping rather than raising if FRED declines the request —
    a few series have no initial-release history. The observations still load,
    with a NULL first_published_date, and mart_macro_series_daily excludes such
    rows from the point-in-time join rather than guessing at a lag.
    """
    params: dict[str, Any] = {
        "series_id": series_id,
        "output_type": 4,
        "realtime_start": EARLIEST_REALTIME,
        "realtime_end": LATEST_REALTIME,
    }
    if start:
        params["observation_start"] = start.isoformat()

    try:
        observations = client.get("/series/observations", params).get("observations") or []
    except requests.HTTPError as exc:
        logger.warning(
            f"  {series_id}: no initial-release history available ({exc}); "
            "first_published_date will be NULL"
        )
        return {}

    releases: dict[date, date] = {}
    for observation in observations:
        observation_date = _to_date(observation.get("date"))
        published = _to_date(observation.get("realtime_start"))
        if observation_date is None or published is None:
            continue
        # An observation can be re-released; keep the EARLIEST, which is the
        # first time anyone could have seen a number for this period. min()
        # rather than "last wins" because FRED sorts by observation date, not by
        # realtime date, so ordering cannot be relied on.
        previous = releases.get(observation_date)
        releases[observation_date] = min(published, previous) if previous else published

    return releases


def ingest_series(
    client: FredClient, series_id: str, start: date | None = None
) -> tuple[int, int, int]:
    """
    Ingest one series. Returns (metadata_rows, observation_rows, missing_values).
    """
    ingested_at = datetime.now(timezone.utc)

    metadata = fetch_series_metadata(client, series_id)
    series_frame = pd.DataFrame([{
        "series_id": metadata["id"],
        "source": SOURCE_NAME,
        "title": metadata.get("title"),
        "frequency": metadata.get("frequency"),
        "frequency_short": metadata.get("frequency_short"),
        "units": metadata.get("units"),
        "units_short": metadata.get("units_short"),
        "seasonal_adjustment": metadata.get("seasonal_adjustment"),
        "seasonal_adjustment_short": metadata.get("seasonal_adjustment_short"),
        "observation_start": _to_date(metadata.get("observation_start")),
        "observation_end": _to_date(metadata.get("observation_end")),
        "last_updated": metadata.get("last_updated"),
        "notes": metadata.get("notes"),
        "ingested_at": ingested_at,
    }])

    # The series row must land before its observations: raw.macro_observations
    # has a composite foreign key onto it, so the reverse order fails the whole
    # batch on the first insert.
    metadata_rows = upsert_dataframe(
        series_frame,
        target="raw.macro_series",
        columns=SERIES_COLUMNS,
        conflict_columns=SERIES_CONFLICT_COLUMNS,
    )

    observations = fetch_observations(client, series_id, start)
    first_releases = fetch_first_release_dates(client, series_id, start)

    rows = []
    missing = 0
    for observation in observations:
        observation_date = _to_date(observation.get("date"))
        if observation_date is None:
            continue
        value = _to_value(observation.get("value"))
        if value is None:
            missing += 1
        rows.append({
            "series_id": series_id,
            "observation_date": observation_date,
            "source": SOURCE_NAME,
            "value": value,
            "first_published_date": first_releases.get(observation_date),
            # Which vintage `value` came from. FRED reports it per row on the
            # default response and it is the same date on every row — the date
            # of this fetch.
            "vintage_date": _to_date(observation.get("realtime_start")),
            "ingested_at": ingested_at,
        })

    if not rows:
        logger.warning(f"  {series_id}: no observations returned")
        return metadata_rows, 0, 0

    observation_rows = upsert_dataframe(
        pd.DataFrame(rows),
        target="raw.macro_observations",
        columns=OBSERVATION_COLUMNS,
        conflict_columns=OBSERVATION_CONFLICT_COLUMNS,
    )
    return metadata_rows, observation_rows, missing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest FRED macroeconomic series into raw Postgres"
    )
    parser.add_argument(
        "--series",
        nargs="+",
        default=DEFAULT_SERIES,
        help="FRED series IDs (default: 10 headline indicators)",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=None,
        help="Earliest observation date to fetch, YYYY-MM-DD. Default: all history.",
    )
    args = parser.parse_args()

    logger.info(f"Starting FRED ingestion | {len(args.series)} series | start={args.start}")

    client = FredClient()
    failures: dict[str, str] = {}
    total_observations = 0
    total_missing = 0

    with RunLedger(
        flow_name="fred_macro",
        metadata={"series": args.series, "start": str(args.start) if args.start else None},
    ) as ledger:
        for i, series_id in enumerate(args.series):
            logger.info(f"[{i+1}/{len(args.series)}] Ingesting {series_id}")
            try:
                _, observations, missing = ingest_series(client, series_id, args.start)
                total_observations += observations
                total_missing += missing
                logger.info(
                    f"  {series_id}: {observations} observations"
                    + (f", {missing} missing ('.')" if missing else "")
                )
            except Exception as exc:
                # Collect and continue (ADR-0011). Each series committed in its
                # own transaction, so completed work is durable; the run is
                # still failed below so the ledger never reports SUCCESS for an
                # incomplete batch.
                failures[series_id] = _redact(f"{type(exc).__name__}: {exc}")
                logger.error(f"  {series_id}: FAILED — {failures[series_id]}")

        ledger.record_rows(total_observations)

        if failures:
            summary = "; ".join(f"{s} ({e})" for s, e in failures.items())
            raise PartialIngestionError(
                f"{len(failures)}/{len(args.series)} series failed: {summary}. "
                f"{total_observations} observations from "
                f"{len(args.series) - len(failures)} successful series were committed."
            )

    logger.info(
        f"FRED ingestion complete | observations={total_observations} "
        f"| missing={total_missing}"
    )


if __name__ == "__main__":
    main()
