"""
Prefect retry behaviour, proven by forcing failures rather than trusting the decorator.

ADR-0005 decides that retries are LAYERED and that the two layers must not
compound blindly:

  - tenacity, inside each adapter, retries a single HTTP request 3 times;
  - Prefect, at the task level, retries a whole task 2 times;
  - and PartialIngestionError is NEVER retried, because under ADR-0011 it is
    raised only after every ticker has been attempted and the successful ones
    committed. Retrying it re-runs the entire batch to re-attempt a failure that
    is deterministic — a delisted symbol stays delisted — while re-paying
    Polygon's five-requests-per-minute rate limit.

`retries=2` on the decorator is one line and reads as obviously correct. The
`retry_condition_fn` beside it is the part that carries the actual decision, and
deleting it would not fail any other test in this suite: the flow would still
run, still succeed on a clean day, and only burn API budget on the days that
already went wrong. So both halves are pinned here.

The tasks are re-derived with `retry_delay_seconds=0`. The production values (30s)
are correct for a real transient fault and would make this file take minutes.
Retry COUNT and CONDITION are what is under test; the delay is not.
"""

import datetime as dt

import pytest
from prefect import flow

from orchestration.flows.daily_ingest import ingest_prices
from src.ingestion.__main__ import PartialIngestionError

pytestmark = pytest.mark.integration

START = dt.date(2026, 6, 1)
END = dt.date(2026, 6, 5)
TICKERS = ["ZZRETRY"]

#: retry_delay_seconds=0 only. retries and retry_condition_fn are inherited from
#: the production task, so this exercises the real predicate rather than a copy.
FAST = ingest_prices.with_options(retry_delay_seconds=0)


@flow(name="retry-harness")
def _harness() -> int:
    return FAST("polygon", TICKERS, START, END, "00000000-0000-0000-0000-000000000000")


def test_transient_failure_is_retried_and_then_succeeds(monkeypatch):
    """
    A ConnectionError on the first attempt must not fail the task.

    This is the case retries exist for: a dropped connection or a killed
    process, which no individual request-level retry inside the adapter could
    have seen, because the adapter never got that far.
    """
    calls = {"n": 0}

    def flaky(source, tickers, start, end, parent_run_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("simulated transient network failure")
        return 42

    monkeypatch.setattr("orchestration.flows.daily_ingest.run_ingestion", flaky)

    assert _harness() == 42
    assert calls["n"] == 2, "task should have been attempted exactly twice"


def test_partial_ingestion_error_is_never_retried(monkeypatch):
    """
    The half of the policy that a bare `retries=2` would get wrong.

    ADR-0011's collect-and-continue loop has already run to completion by the
    time this exception is raised — every ticker attempted, the good ones
    committed, the failures named. A retry re-runs all of them to re-attempt a
    deterministic failure.

    Asserting calls == 1 is the whole point: with retry_condition_fn removed
    this is 3, and nothing else in the test suite would notice.
    """
    calls = {"n": 0}

    def always_partial(source, tickers, start, end, parent_run_id=None):
        calls["n"] += 1
        raise PartialIngestionError("1/1 tickers failed: ZZRETRY (delisted). 0 rows committed.")

    monkeypatch.setattr("orchestration.flows.daily_ingest.run_ingestion", always_partial)

    with pytest.raises(PartialIngestionError):
        _harness()

    assert calls["n"] == 1, (
        "PartialIngestionError must not be retried — ADR-0011 already handled "
        "the per-ticker failures, so a retry only re-pays the rate limit"
    )


def test_retries_are_exhausted_and_the_task_still_fails(monkeypatch):
    """
    Non-vacuity guard for the first test.

    Without this, a retry_condition_fn that returned True unconditionally AND a
    task that never truly failed would both look correct. This pins the upper
    bound: 1 initial attempt + 2 retries = 3, and then the failure propagates
    rather than being swallowed.
    """
    calls = {"n": 0}

    def always_transient(source, tickers, start, end, parent_run_id=None):
        calls["n"] += 1
        raise ConnectionError("simulated permanent outage")

    monkeypatch.setattr("orchestration.flows.daily_ingest.run_ingestion", always_transient)

    with pytest.raises(ConnectionError):
        _harness()

    assert calls["n"] == 3, "expected 1 attempt + 2 retries"
