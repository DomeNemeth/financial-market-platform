# ADR-0005: Prefect for orchestration, and what a dbt failure means to a flow

**Date:** 2026-08-04
**Status:** Accepted

## Context

By the end of Phase 5 the platform has four entrypoints that must run in order
and in a specific relationship to each other:

```
python -m src.ingestion --source polygon
python -m src.ingestion --source yahoo
python -m src.ingestion.fred
dbt build
```

Run by hand this is fine. Run nightly it is not: something has to sequence them,
retry the parts that fail for transient reasons, record what happened, and not
lie about the result. The pieces that already exist — `RunLedger`, ADR-0011's
collect-and-continue policy, tenacity retries inside each adapter — solve parts
of that, and an orchestrator has to compose with them rather than duplicate or
contradict them.

The genuinely hard question is not "which orchestrator". It is **what a dbt
failure means to a flow that has already committed ingested rows**, and that
question has a sharp edge specific to this project: `dbt build` currently
emits, and will keep emitting, a `WARN` that is correct.
`assert_dividend_factors_have_a_reference_close` reports 138 rows because
corporate actions are ingested from 2020 while prices cover months, and ADR-0003
already decided such dividends are skipped. A flow that treats dbt's exit status
as a boolean would fail every single night on a condition the platform
deliberately accepts — and the fix someone would reach for is deleting the
warning, which is exactly the silent weakening this project keeps trying not to
ship.

## Decision

### 1. Prefect 3, run as a local deployment.

`prefect deploy` plus a local worker process, with the schedule in the
deployment. No Prefect Cloud account, no external dependency, nothing needing
admin rights.

Prefect over Airflow because Airflow's scheduler, webserver, and metadata
database are a larger operational surface than the pipeline they would run;
Prefect flows are ordinary Python functions, so the ingestion code stays callable
and testable without the orchestrator present. Prefect over bare cron or Task
Scheduler because retries, per-task observability, and a run history are the
actual reasons to introduce an orchestrator, and cron has none of them.

Pinned to `prefect>=3.0`. 3.x is what is installed and its deployment API differs
materially from 2.x; the previous `>=2.19` in `pyproject.toml` would have
resolved to either.

### 2. A dbt `WARN` never fails the flow. A dbt `ERROR` or test `FAIL` always does.

The flow does not read dbt's exit code. It parses
`dbt/target/run_results.json` and counts node statuses:

| dbt status | meaning | flow |
|---|---|---|
| `success` / `pass` | fine | continue |
| `warn` | a `severity: warn` test returned rows | **continue**, count recorded |
| `fail` | a data test returned rows | **fail the flow** |
| `error` | compilation or database error | **fail the flow** |
| `skipped` | upstream failed | counted, does not itself fail |

Parsing the artifact rather than trusting the exit code is deliberate. It gives
the *counts* rather than a boolean, so the warning total lands in the ledger's
metadata and a change in it is visible — which is the whole reason
`assert_dividend_factors_have_a_reference_close` is a warning and not a deletion.
It also distinguishes `fail` (a data test found rows) from `error` (dbt could not
run), which are different problems with the same exit code.

**The warning count is recorded, not thresholded.** A tempting refinement is to
fail the flow when warnings *increase*, since ADR-0003's note says the count
"should only shrink as history is backfilled". Rejected: the count legitimately
moves when the ingestion window moves, so the rule would fire on correct
backfills, and a flow that fails on a correct action gets its check removed.
The count is in `pipeline_runs.metadata` where a human or a future dashboard can
trend it.

### 3. dbt runs unless *every* ingest source failed.

If one or two sources fail, dbt still builds. This follows from ADR-0006: Yahoo
exists precisely so Polygon failing is survivable, and after a partial failure
the merged series has genuinely changed and should be rebuilt. Rebuilding also
re-runs 143 tests over the data that *is* there, which has value on a day nothing
new landed.

If **all** sources fail, dbt is skipped. That is the signature of a systemic
problem — expired key, network down, database unreachable — where nothing new
landed, so a rebuild changes nothing, and where letting dbt run risks stacking a
downstream failure (`assert_no_missing_trading_days` finding yesterday's gap) on
top of the actual cause. The root cause should be the thing reported, not the
loudest consequence of it.

### 4. One flow run produces one parent ledger row and a child row per step.

Migration 0006 adds a nullable, self-referencing `parent_run_id`. The flow opens
a `daily_ingest` row and passes its id to each step's `RunLedger`.

This keeps two questions answerable from `pipeline_runs` alone — "did last
night's run succeed?" from the parent, "which source failed and why?" from the
children — without either flattening everything into one row's JSONB or forcing
a correlation by timestamp, which stops working the moment two runs overlap.

`parent_run_id` stays NULL for CLI-initiated runs. The CLIs remain first-class:
the flow is a convenience over them, not a replacement, and a hand-run backfill
must record itself exactly as it always has. The flow calls `run_ingestion()` and
`run_fred_ingestion()` — the same functions the CLIs call — rather than shelling
out, so the failure policy has exactly one implementation.

### 5. Retries are layered, and `PartialIngestionError` is never retried.

Two retry mechanisms now exist and they must not compound blindly.

- **tenacity, inside each adapter**, retries a single HTTP request 3 times with
  exponential backoff. It handles a dropped connection or a 503 on one call.
- **Prefect, at the task level**, retries a whole task 2 times. It handles a task
  that died for a reason no individual request could see — the database being
  briefly unreachable, the process being starved.

Left alone these multiply: 3 tenacity attempts inside 3 Prefect attempts is 9
requests per ticker. Worse, they would compound over the *wrong* failure. Under
ADR-0011 a run where one ticker is delisted raises `PartialIngestionError`
**after** every other ticker has succeeded and committed. Retrying that re-runs
all ten tickers — idempotent, so harmless to the data, but on Polygon's
five-requests-per-minute tier it re-pays two minutes of rate-limit wait to
re-attempt a failure that is deterministic and will fail again.

So each ingest task carries a `retry_condition_fn` that declines to retry
`PartialIngestionError`. The rule it encodes: **retry infrastructure, never
retry a data condition the collect-and-continue loop has already handled.**

## Consequences

Good:

- The nightly run is honest by construction. `pipeline_runs` never reports
  `SUCCESS` for a flow whose dbt build failed or whose sources all died, and the
  parent/child rows say precisely which part broke.
- The permanent 138-row warning stops being a problem to work around and becomes
  a number that gets recorded every night.
- Ingestion stays runnable without Prefect. The flow imports the same functions
  the CLIs use, so nothing about the failure policy is orchestrator-specific and
  the integration tests keep testing the real path.
- A flaky vendor no longer blocks the transform, which is the fragility ADR-0006's
  fallback was introduced to remove.

Bad:

- Two retry layers is genuinely more to reason about than one, and the
  interaction is only correct because of an explicit `retry_condition_fn` that a
  future reader could delete without any test failing on the same day. The
  reasoning is written at the call site as well as here.
- Parsing `run_results.json` couples the flow to a dbt artifact whose schema dbt
  may change across minor versions. Accepted: the alternative is a boolean, and
  ADR-0010 already pins dbt to the 1.11 line.
- A local worker is a process someone has to keep running. Honest about what a
  laptop deployment is; it is not a claim of high availability.

Neutral:

- FRED is re-fetched daily despite most of its series being monthly or
  quarterly. Loads are idempotent so this is ~20 wasted requests against a
  120/minute limit, and it means a revision is picked up the day it appears
  rather than up to a week later. One flow and one schedule is worth more than
  the saved calls.

## Alternatives Considered

**Airflow.** The industry default, and the thing a job description is more
likely to name. Rejected on operational weight: a scheduler, a webserver, and a
metadata database to run four commands nightly, plus a DAG-definition style that
would make the ingestion code hard to call directly from a test. Prefect's flows
are the functions themselves.

**Windows Task Scheduler triggering the CLIs, with no orchestrator.** Genuinely
tempting — no long-lived worker, and honest about how a laptop actually runs
things. Rejected because retries, run history, and per-task observability are
the reasons to orchestrate at all, and this provides none of them; the run ledger
would be the only record, and it cannot show that a task was retried.

**Prefect Cloud.** Best-looking for a portfolio demo, with a real UI. Rejected:
it adds an external account and a network dependency to a project whose entire
point is that it runs locally and reproducibly, and the free tier's retention
would silently drop the run history it was adopted for.

**Trusting `dbt`'s exit code instead of parsing `run_results.json`.** Simpler by
a dozen lines. Rejected because it collapses `warn`, `fail`, and `error` into one
bit, and this project has a permanent, correct warning — so the simple version
either fails nightly or requires suppressing the warning at the dbt end, which
means weakening the test.

**Making the dbt step a dbt Cloud job.** Not considered seriously; ADR-0001 and
ADR-0010 already commit to local dbt-core against local Postgres.
