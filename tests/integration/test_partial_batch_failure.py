"""
Partial-batch failure behaviour (ADR-0011).

The policy is collect-and-continue, then fail the run. It exists to get two
properties at once that the two conventional answers each give up one of:

  - fail-fast discards every ticker that would have succeeded *after* the
    failure, which on a 5-request/minute tier means re-paying minutes of
    rate-limit wait on every retry;
  - log-and-continue records SUCCESS for an incomplete batch, which makes the
    run ledger lie about completeness — the one thing it exists to answer.

So the assertions here are deliberately paired: successful tickers' rows must be
committed AND the run must still be recorded FAILED. Either alone is the wrong
behaviour.

The whole CLI entrypoint is exercised, not a helper, because the interaction
between the loop, the ledger context manager, and the raised error is the thing
under test.
"""

import datetime as dt

import pandas as pd
import pytest
import requests
from sqlalchemy import text

import src.ingestion.__main__ as ingest_cli
from src.common.calendar import trading_days
from src.ingestion.adapters.polygon import PolygonAdapter

pytestmark = pytest.mark.integration

TEST_SOURCE = "test_partial"
START = dt.date(2026, 6, 1)
END = dt.date(2026, 6, 5)

GOOD_A, BAD, GOOD_B = "ZZGOODA", "ZZBAD", "ZZGOODB"


class FakeAdapter(PolygonAdapter):
    """
    Real validate/write_parquet/load_to_postgres; only the network call is faked.

    Subclassing rather than mocking keeps the actual persistence path under test,
    so a failure here means the real load path misbehaved, not that a stub did.
    """

    SOURCE_NAME = TEST_SOURCE

    #: Tickers whose fetch raises, simulating a vendor error for one symbol.
    failing: set[str] = {BAD}

    def fetch(self, ticker: str, start_date: dt.date, end_date: dt.date) -> pd.DataFrame:
        if ticker in self.failing:
            raise requests.HTTPError(f"503 Server Error: simulated vendor failure for {ticker}")

        return pd.DataFrame([
            {
                "ticker": ticker,
                "trading_date": session,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1000.0,
                "vwap": 100.2,
                "trade_count": 50,
                "source": self.SOURCE_NAME,
                "ingested_at": dt.datetime.now(dt.timezone.utc),
            }
            for session in trading_days(start_date, end_date)
        ])


@pytest.fixture
def fast_cli(monkeypatch):
    """Swap in the fake adapter and drop the 12s inter-ticker rate-limit sleep."""
    monkeypatch.setattr(ingest_cli, "PolygonAdapter", FakeAdapter)
    monkeypatch.setattr(ingest_cli, "RATE_LIMIT_SLEEP", 0)


@pytest.fixture
def clean_state(db_engine):
    def _cleanup():
        with db_engine.connect() as conn:
            conn.execute(
                text("DELETE FROM raw.prices WHERE source = :s"), {"s": TEST_SOURCE}
            )
            conn.execute(
                text("""
                    DELETE FROM public.pipeline_runs
                    WHERE flow_name = 'polygon_ohlcv'
                      AND metadata::text LIKE '%ZZ%'
                """)
            )
            conn.commit()

    _cleanup()
    yield
    _cleanup()


def run_cli(monkeypatch, tickers: list[str]) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["src.ingestion", "--tickers", *tickers, "--start", str(START), "--end", str(END)],
    )
    ingest_cli.main()


def latest_run(conn):
    return conn.execute(
        text("""
            SELECT status, rows_ingested, error_message
            FROM public.pipeline_runs
            WHERE flow_name = 'polygon_ohlcv' AND metadata::text LIKE '%ZZ%'
            ORDER BY started_at DESC LIMIT 1
        """)
    ).fetchone()


def stored_tickers(conn) -> set[str]:
    rows = conn.execute(
        text("SELECT DISTINCT ticker FROM raw.prices WHERE source = :s"), {"s": TEST_SOURCE}
    ).fetchall()
    return {r[0] for r in rows}


# ------------------------------------------------------------------ the policy


def test_partial_failure_raises_after_processing_every_ticker(
    monkeypatch, fast_cli, clean_state
):
    """
    The failing ticker is in the middle, so a fail-fast implementation would
    never reach GOOD_B. Reaching it is the whole point of collect-and-continue.
    """
    with pytest.raises(ingest_cli.PartialIngestionError) as exc:
        run_cli(monkeypatch, [GOOD_A, BAD, GOOD_B])

    message = str(exc.value)
    assert BAD in message, "the error must name the failed ticker"
    assert "1/3" in message, f"expected a 1-of-3 failure count, got: {message}"


def test_successful_tickers_are_committed_despite_the_failure(
    monkeypatch, db_engine, fast_cli, clean_state
):
    """Work already done stays done — that is what makes a retry cheap."""
    with pytest.raises(ingest_cli.PartialIngestionError):
        run_cli(monkeypatch, [GOOD_A, BAD, GOOD_B])

    with db_engine.connect() as conn:
        assert stored_tickers(conn) == {GOOD_A, GOOD_B}


def test_run_is_recorded_failed_not_success(monkeypatch, db_engine, fast_cli, clean_state):
    """
    The other half of the policy. An incomplete batch must never be recorded
    SUCCESS, or the ledger stops being usable as a completeness signal.
    """
    with pytest.raises(ingest_cli.PartialIngestionError):
        run_cli(monkeypatch, [GOOD_A, BAD, GOOD_B])

    with db_engine.connect() as conn:
        status, rows_ingested, error_message = latest_run(conn)

    assert status == "FAILED"
    assert BAD in error_message, "the ledger must record which ticker failed"
    # rows_ingested on a FAILED run means "what landed", not "what was expected".
    expected = len(trading_days(START, END)) * 2
    assert rows_ingested == expected, f"expected {expected} committed rows, got {rows_ingested}"


def test_total_failure_reports_zero_rows(monkeypatch, db_engine, fast_cli, clean_state):
    """A batch where everything fails is the same shape, with nothing committed."""
    monkeypatch.setattr(FakeAdapter, "failing", {GOOD_A, BAD})

    with pytest.raises(ingest_cli.PartialIngestionError) as exc:
        run_cli(monkeypatch, [GOOD_A, BAD])

    assert "2/2" in str(exc.value)
    with db_engine.connect() as conn:
        assert stored_tickers(conn) == set()
        status, rows_ingested, _ = latest_run(conn)
    assert status == "FAILED"
    assert rows_ingested == 0


def test_clean_batch_still_succeeds(monkeypatch, db_engine, fast_cli, clean_state):
    """
    Non-vacuity guard: with no failures the run must record SUCCESS. Without
    this, an implementation that failed every run would pass the tests above.
    """
    run_cli(monkeypatch, [GOOD_A, GOOD_B])  # must not raise

    with db_engine.connect() as conn:
        status, rows_ingested, error_message = latest_run(conn)
        assert stored_tickers(conn) == {GOOD_A, GOOD_B}

    assert status == "SUCCESS"
    assert error_message is None
    assert rows_ingested == len(trading_days(START, END)) * 2


def test_retry_after_partial_failure_is_idempotent(
    monkeypatch, db_engine, fast_cli, clean_state
):
    """
    The practical payoff. Re-running after fixing the failure must not duplicate
    the rows that already landed — which is only true because loads upsert.
    """
    with pytest.raises(ingest_cli.PartialIngestionError):
        run_cli(monkeypatch, [GOOD_A, BAD, GOOD_B])

    with db_engine.connect() as conn:
        before = conn.execute(
            text("SELECT count(*), sum(id) FROM raw.prices WHERE source = :s"),
            {"s": TEST_SOURCE},
        ).fetchone()

    # The vendor issue clears; the same batch is retried in full.
    monkeypatch.setattr(FakeAdapter, "failing", set())
    run_cli(monkeypatch, [GOOD_A, BAD, GOOD_B])

    with db_engine.connect() as conn:
        after = conn.execute(
            text("SELECT count(*), sum(id) FROM raw.prices WHERE source = :s"),
            {"s": TEST_SOURCE},
        ).fetchone()
        status, _, _ = latest_run(conn)

    sessions = len(trading_days(START, END))
    assert before[0] == sessions * 2, "first run should have committed two tickers"
    assert after[0] == sessions * 3, "retry should add only the previously-failed ticker"
    # The originally-successful rows kept their ids: updated, not re-inserted.
    assert after[1] > before[1]
    assert status == "SUCCESS", "a fully successful retry must clear the FAILED status"
