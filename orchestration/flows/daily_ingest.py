"""
The nightly flow: ingest every source, then rebuild and test the warehouse.

    .venv\\Scripts\\python.exe -m orchestration.flows.daily_ingest

ADR-0005 is the decision record. The four rules that are easy to break by
accident, all of them enforced below rather than left to convention:

  1. A dbt WARN never fails the flow; a dbt ERROR or test FAIL always does.
     The flow reads run_results.json rather than dbt's exit code, because this
     project has a permanent and CORRECT warning
     (assert_dividend_factors_have_a_reference_close, 138 rows) and a boolean
     exit status cannot tell it apart from a real failure.

  2. dbt runs unless EVERY source failed. One vendor dying is survivable — that
     is what ADR-0006's fallback is for — and after a partial failure the merged
     series has genuinely changed. All sources dying means nothing landed, and
     running dbt would stack a downstream gap failure on top of the real cause.

  3. One flow run writes one parent ledger row and a child row per step
     (migration 0006), so pipeline_runs answers both "did last night work" and
     "which source broke".

  4. PartialIngestionError is NEVER retried. Under ADR-0011 it is raised after
     every ticker has been attempted and the good ones committed, so a retry
     re-pays Polygon's rate limit to re-attempt a deterministic failure.

The tasks call run_ingestion()/run_fred_ingestion() — the same functions the
CLIs call — rather than shelling out, so the failure policy has exactly one
implementation and the CLIs stay first-class entrypoints.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from prefect import flow, task
from prefect.logging import get_run_logger

from src.common.config import settings
from src.common.logging import configure_logging
from src.common.tls import enable_system_trust_store
from src.ingestion.__main__ import DEFAULT_TICKERS, PartialIngestionError, run_ingestion
from src.ingestion.fred import DEFAULT_SERIES, run_fred_ingestion
from src.ingestion.fred import PartialIngestionError as FredPartialIngestionError
from src.ingestion.run_ledger import RunLedger

configure_logging()
# Must run before any outbound HTTPS request. See src/common/tls.py.
enable_system_trust_store()
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DBT_PROJECT_DIR = REPO_ROOT / "dbt"
RUN_RESULTS = DBT_PROJECT_DIR / "target" / "run_results.json"

#: dbt node statuses that mean the build did not do its job. `fail` is a data
#: test that returned rows; `error` is dbt being unable to run at all. Different
#: problems, same consequence, and dbt's exit code cannot distinguish them.
DBT_FAILURE_STATUSES = {"fail", "error"}


def _is_retryable(task, task_run, state) -> bool:
    """
    Prefect retry predicate: retry infrastructure, never a handled data condition.

    PartialIngestionError means ADR-0011's collect-and-continue loop already ran
    to completion — every ticker was attempted, the good ones committed, and the
    failures are recorded by name. Retrying re-runs the whole batch. The loads
    are idempotent so no data is harmed, but on Polygon's five-requests-per-minute
    tier it re-pays two minutes of rate-limit wait to re-attempt a failure that
    is deterministic (a delisted symbol will still be delisted) and will fail
    again on every attempt.

    Everything else — a dropped database connection, a killed process — is worth
    another go. Note that transient HTTP failures are already retried three times
    by tenacity INSIDE the adapter, so anything reaching here has survived that.
    """
    try:
        exc = state.result(raise_on_failure=False)
    except Exception as raised:
        exc = raised
    return not isinstance(exc, (PartialIngestionError, FredPartialIngestionError))


@task(retries=2, retry_delay_seconds=30, retry_condition_fn=_is_retryable)
def ingest_prices(source: str, tickers: list[str], start: date, end: date,
                  parent_run_id: str) -> int:
    """Ingest one price vendor. Writes its own child ledger row."""
    log = get_run_logger()
    log.info(f"Ingesting {source} prices | {start} → {end} | {len(tickers)} tickers")
    return run_ingestion(source, tickers, start, end, parent_run_id=parent_run_id)


@task(retries=2, retry_delay_seconds=30, retry_condition_fn=_is_retryable)
def ingest_macro(series: list[str], start: date | None, parent_run_id: str) -> int:
    """Ingest the FRED macro series. Writes its own child ledger row."""
    log = get_run_logger()
    log.info(f"Ingesting {len(series)} FRED series")
    return run_fred_ingestion(series, start, parent_run_id=parent_run_id)


class DbtBuildError(Exception):
    """Raised when dbt reported a node with status `fail` or `error`."""


def tracked_tickers() -> list[str]:
    """
    The universe to ingest: every ticker the platform holds reference data for.

    NOT src.ingestion.__main__.DEFAULT_TICKERS. That list is a convenience for
    someone typing a command; using it here made the flow's first run ingest
    GOOGL, AMZN, META, TSLA and JNJ — five securities with no row in
    raw.security_master. Their bars resolve to no security_id, so they can never
    reach the mart, and they fail assert_every_price_bar_resolves_to_a_security
    on the next build. The flow would have been manufacturing a data-quality
    failure every night.

    Deriving the universe from the security master makes the ordering explicit
    and self-maintaining: reference data first, prices second. To track a new
    security, ingest its master row (`python -m src.ingestion.security_master
    --tickers XYZ`) and the nightly flow picks it up with no edit here.

    Falls back to DEFAULT_TICKERS only when the master is empty, which is a
    first-run bootstrap rather than a steady state.
    """
    from sqlalchemy import text

    from src.common.database import engine

    with engine.connect() as conn:
        rows = conn.execute(
            text("select distinct ticker from raw.security_master order by ticker")
        ).scalars().all()

    if not rows:
        logger.warning(
            "raw.security_master is empty — falling back to DEFAULT_TICKERS. "
            "Ingest the security master first, or these bars will not resolve."
        )
        return list(DEFAULT_TICKERS)
    return list(rows)


def _child_rows_ingested(parent_run_id: str) -> int:
    """
    Total rows actually committed by this flow run's steps.

    Read back from the child ledger rows rather than accumulated in memory,
    because a step that raised still committed whatever it had processed
    (ADR-0011) and its return value never reaches the caller. The child row is
    the only place that count survives.

    dbt_build contributes 0 by construction — it records no rows_ingested,
    since it transforms rather than ingests.
    """
    from sqlalchemy import text

    from src.common.database import engine

    with engine.connect() as conn:
        return conn.execute(
            text("""
                select coalesce(sum(rows_ingested), 0)
                from public.pipeline_runs
                where parent_run_id = CAST(:parent AS uuid)
            """),
            {"parent": parent_run_id},
        ).scalar() or 0


def _dbt_env() -> dict[str, str]:
    """
    Build the environment dbt needs, since this process does not already have it.

    dbt's `env_var()` reads REAL OS environment variables. pydantic-settings
    reads `.env` into the `Settings` object and does NOT export anything to
    os.environ, so a subprocess inherits nothing useful — dbt fails at parse
    time with "Env var required but not provided: 'POSTGRES_USER'".

    This is the same job scripts/dbt.ps1 does by shelling through `dotenv run`,
    done in-process instead. The wrapper is not used here because it would add a
    PowerShell dependency to the flow and it always appends --project-dir, which
    this passes explicitly.

    Keep in sync with dbt/profiles.yml.example. A variable missing here fails
    loudly at dbt parse time rather than silently connecting somewhere else,
    because the profile has no default for user, password, or dbname.
    """
    env = os.environ.copy()
    env.update({
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password,
    })
    return env


def _summarise_run_results(path: Path, not_before: float, exit_code: int) -> dict[str, Any]:
    """
    Read dbt's run_results.json and count node statuses.

    Parsed rather than inferred from the exit code, because the exit code is one
    bit and this project needs three distinctions from it: a WARN that must not
    fail the flow, a FAIL that must, and an ERROR that must for a different
    reason. See ADR-0005.

    ------------------------------------------------------------------
    THE FRESHNESS CHECK IS THE IMPORTANT PART OF THIS FUNCTION.

    dbt writes run_results.json only if it got far enough to run nodes. A
    profile error, a connection failure, or a compilation error leaves LAST
    RUN'S FILE SITTING THERE — and parsing that reports a clean build from a run
    that never happened.

    This is not hypothetical: the first version of this flow checked only that
    the file existed, and reported "dbt build clean | 1 nodes | warnings=0"
    for a dbt invocation that had died with
    "Env var required but not provided: 'POSTGRES_USER'". A flow whose entire
    purpose is to not lie about the result had lied about the result on its
    first real run.

    So the artifact must be newer than the subprocess that was supposed to write
    it. `not_before` is captured immediately before dbt is launched.
    """
    if not path.exists():
        raise DbtBuildError(
            f"dbt wrote no run_results.json at {path} (exit code {exit_code}). "
            "It failed before any node ran — usually a profile, connection, or "
            "compilation problem."
        )

    if path.stat().st_mtime < not_before:
        raise DbtBuildError(
            f"dbt did not write a fresh run_results.json (exit code {exit_code}); "
            f"the file at {path} predates this run and describes a previous one. "
            "dbt failed before executing any node — check the captured stderr for "
            "a profile, connection, or compilation error."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results") or []

    counts: dict[str, int] = {}
    for node in results:
        status = node.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    failures = [
        {
            "node": node.get("unique_id"),
            "status": node.get("status"),
            "message": (node.get("message") or "")[:300],
        }
        for node in results
        if node.get("status") in DBT_FAILURE_STATUSES
    ]

    return {
        "nodes": len(results),
        "status_counts": counts,
        "warn_count": counts.get("warn", 0),
        "failures": failures,
    }


@task(retries=1, retry_delay_seconds=15)
def dbt_build(parent_run_id: str) -> dict[str, Any]:
    """
    Run `dbt build` and decide what its result means. Writes a child ledger row.

    Invoked as `python -m dbt.cli.main` from the project's own venv rather than
    through scripts/dbt.ps1, with the database environment supplied explicitly
    by _dbt_env(). Going through the wrapper would add a PowerShell dependency
    to the flow, and the wrapper always appends --project-dir, which this passes
    itself.
    """
    log = get_run_logger()

    with RunLedger(
        flow_name="dbt_build",
        metadata={"project_dir": str(DBT_PROJECT_DIR)},
        parent_run_id=parent_run_id,
    ) as ledger:
        # Captured BEFORE launching dbt, so the freshness check below can tell a
        # file this run wrote from one left behind by the previous run.
        launched_at = time.time()

        completed = subprocess.run(
            [sys.executable, "-m", "dbt.cli.main", "build",
             "--project-dir", str(DBT_PROJECT_DIR)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=_dbt_env(),
        )
        log.info(f"dbt exited {completed.returncode}")
        if completed.returncode != 0:
            # dbt's own diagnostics, which are far more specific than anything
            # reconstructable from run_results.json — and are the only thing
            # available at all when it died before writing one.
            log.error(f"dbt stderr:\n{completed.stderr[-2000:]}")
            log.error(f"dbt stdout tail:\n{completed.stdout[-2000:]}")

        summary = _summarise_run_results(RUN_RESULTS, launched_at, completed.returncode)
        summary["exit_code"] = completed.returncode
        ledger.record_metadata(**summary)
        # rows_ingested is deliberately left at 0. dbt ingests nothing — it
        # transforms what ingestion already landed — and putting the node count
        # here would make `select sum(rows_ingested)` over a flow's children
        # silently add 143 dbt nodes to a count of price bars. The node count
        # lives in metadata, where it is labelled.

        if summary["warn_count"]:
            # Expected and correct. assert_dividend_factors_have_a_reference_close
            # reports ~138 rows because corporate actions are ingested from 2020
            # while prices cover months, and ADR-0003 already decided those
            # dividends are skipped. Recorded so the number can be trended, NOT
            # thresholded — the count legitimately moves when the ingestion
            # window moves, so a "must not increase" rule would fire on correct
            # backfills. See ADR-0005.
            log.warning(
                f"dbt build produced {summary['warn_count']} warning(s). "
                "This does not fail the flow."
            )

        if summary["failures"]:
            shown = summary["failures"][:5]
            names = ", ".join(f"{f['node']} ({f['status']})" for f in shown)
            hidden = len(summary["failures"]) - len(shown)
            more = f" (+{hidden} more)" if hidden else ""
            raise DbtBuildError(
                f"{len(summary['failures'])} dbt node(s) failed: {names}{more}"
            )

    log.info(
        f"dbt build clean | {summary['nodes']} nodes | "
        f"warnings={summary['warn_count']}"
    )
    return summary


@flow(name="daily-ingest")
def daily_ingest(
    tickers: list[str] | None = None,
    series: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    macro_start: date | None = None,
) -> dict[str, Any]:
    """
    Ingest every source, then rebuild and test the warehouse.

    Defaults to a trailing 5-day window rather than a single day. A daily flow
    that fetches only today's bar leaves a permanent hole whenever a run is
    missed, and the loads are idempotent (INSERT ... ON CONFLICT), so re-fetching
    four days that are already present costs one API call per ticker and repairs
    a gap that would otherwise need a manual backfill.
    """
    log = get_run_logger()

    tickers = tickers or tracked_tickers()
    series = series or DEFAULT_SERIES
    end = end or date.today()
    start = start or (end - timedelta(days=5))

    with RunLedger(
        flow_name="daily_ingest",
        metadata={
            "tickers": tickers,
            "series": series,
            "start": str(start),
            "end": str(end),
        },
    ) as flow_ledger:
        parent = flow_ledger.run_id

        # submit() so the three ingestions run concurrently. They touch
        # different vendors and, apart from raw.prices' upsert, different
        # tables; the price loads conflict only on distinct (ticker, date,
        # SOURCE) keys, so no two of these tasks contend for the same row.
        futures = {
            "polygon": ingest_prices.submit("polygon", tickers, start, end, parent),
            "yahoo": ingest_prices.submit("yahoo", tickers, start, end, parent),
            "fred": ingest_macro.submit(series, macro_start, parent),
        }

        results: dict[str, Any] = {}
        failures: dict[str, str] = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                # Collect and continue, exactly as ADR-0011 does per ticker —
                # applied here per SOURCE. One vendor failing must not prevent
                # the others being reported, or the flow degrades to fail-fast
                # at a coarser grain than the CLI it wraps.
                failures[name] = f"{type(exc).__name__}: {exc}"
                log.error(f"{name} ingestion FAILED — {exc}")

        succeeded = [n for n in futures if n not in failures]

        # ADR-0005 rule 3. dbt runs unless EVERY source failed.
        dbt_summary: dict[str, Any] | None = None
        dbt_skipped_reason: str | None = None
        if succeeded:
            dbt_summary = dbt_build(parent)
        else:
            # Every source down is systemic — expired key, network, database.
            # Nothing new landed, so a rebuild changes nothing, and letting dbt
            # run risks failing assert_no_missing_trading_days on the gap the
            # outage just created and burying the actual cause under it.
            dbt_skipped_reason = (
                "every ingest source failed, so nothing new landed and a rebuild "
                "would only surface the outage as a downstream data-quality failure"
            )
            log.error(f"Skipping dbt build: {dbt_skipped_reason}")

        # Summed from the CHILD ledger rows rather than from `results`.
        #
        # `results` only holds return values of tasks that completed, so a
        # source that raised contributes nothing — even though ADR-0011
        # guarantees its successful items were committed before it raised. The
        # first version of this summed `results` and reported 36 rows for a run
        # whose FRED child had committed 36,245 observations, which is precisely
        # the kind of understated-but-plausible number this ledger exists to
        # avoid.
        #
        # The children know what actually landed, because each wrote its own row
        # inside its own ledger context before the exception propagated.
        flow_ledger.record_rows(_child_rows_ingested(parent))
        flow_ledger.record_metadata(
            rows_by_source=results,
            failed_sources=failures,
            dbt=dbt_summary,
            dbt_skipped_reason=dbt_skipped_reason,
        )

        if failures:
            raise PartialIngestionError(
                f"{len(failures)}/{len(futures)} source(s) failed: "
                + "; ".join(f"{n} ({e})" for n, e in failures.items())
                + (f". {len(succeeded)} source(s) committed and dbt rebuilt."
                   if succeeded else ". dbt was skipped.")
            )

    return {"rows_by_source": results, "dbt": dbt_summary}


if __name__ == "__main__":
    daily_ingest()
