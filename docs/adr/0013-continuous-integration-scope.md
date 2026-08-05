# ADR-0013: What CI Runs, and What It Deliberately Does Not

**Date:** 2026-08-05
**Status:** Accepted

## Context

Until Phase 6 this project had no CI. `.github/workflows/` was an empty
directory, and CLAUDE.md had at one point implied CI existed when it did not.
Every claim in this repository about the suite passing was a claim about one
laptop, on one Windows machine, with a warehouse that had accumulated state over
five phases of manual ingestion.

That is a weak guarantee, and it is weak in a specific way that matters here.
This project's tests are unusually dependent on *data* rather than on code
alone: `assert_deadjusted_yahoo_reconciles_to_polygon_raw` needs KLAC's nine
pre-split overlap sessions to exist or its non-vacuity guard fires;
`assert_point_in_time_macro_differs_from_naive` needs macro observations whose
publication date falls after the price window or it proves nothing;
`test_total_return_reconciliation` calls `pytest.skip` outright when
`int_prices_with_adjustments` is empty. A CI run against an empty database would
be green, fast, and completely meaningless — a large number of tests would skip
themselves and report success.

So the question CI has to answer is not "does the code import" but "would this
change break the guarantees the ADRs claim". That forces four decisions.

## Decision

### 1. Four things run on every push and pull request, in this order

```
init.sql → migrations → CI fixtures → dbt build → integration tests → unit tests → ruff
```

The order is the dependency order and is not arbitrary:

| Step | Why it is in CI |
|---|---|
| `docker/postgres/init.sql` then `src.common.migrate` | Proves the schema builds **from empty**. `init.sql` only ever executes on a fresh data directory locally, so on a developer machine it is effectively never re-run after Phase 1. CI is the only place it is exercised, and a migration that works against an evolved local database but not against a clean one would otherwise be discovered by the next person to clone the repo. |
| `scripts/load_ci_fixtures.py` | Gives dbt and the integration tests real rows to work on. Without it every data-dependent test skips and CI is theatre. |
| `.\dbt build` (`--target ci`) | The transform layer *is* the product. 20 of this project's assertions are dbt tests, not pytest tests, and they are the ones that encode ADR-0003, ADR-0006 and ADR-0012. |
| `pytest tests/integration -m "not live_vendor"` | The API contract, the ledger, idempotency, and the partial-failure policy. |
| `pytest -m "not integration"` | The pure units. Last because they are the least likely to fail and the fastest to re-run locally. |
| `ruff check` | Cheap, and it runs last so a formatting nit never masks a real failure by aborting the job early. |

### 2. CI never calls a vendor API

No step in CI makes an outbound request to Polygon, Yahoo, FRED or OpenFIGI.
This is not a cost decision — all four are free at the volumes involved. It is a
determinism decision. A vendor outage, a rate limit, or a restated bar would
turn CI red for a reason that has nothing to do with the commit under test, and
a CI signal that is red for reasons outside the diff is a signal people learn to
ignore.

The one test that genuinely requires a live vendor —
`test_split_reconciliation.py`, whose entire value is that it compares our
arithmetic against Polygon's *own* adjusted close as an external oracle — is
marked `live_vendor` and deselected in CI.

**The marker is declared, not incidental.** That test already carried
`skipif(not settings.polygon_api_key)`, and since CI has no key it would have
skipped itself with no further work. That was rejected: an incidental skip is
invisible, and the *next* test someone writes against a live vendor will not
have the skipif, will not be noticed, and will make real network calls from a
runner. `-m "not live_vendor"` makes the boundary a thing you have to opt out of
rather than a thing you have to remember.

Running the live leg is a local, deliberate act:

```powershell
.venv\Scripts\python.exe -m pytest tests/integration -m live_vendor
```

### 3. Fixtures are a snapshot of the real warehouse, not synthetic data

`tests/fixtures/ci/*.csv` is an export of the actual `raw` schema — KLA's real
10-for-1 split on 2026-06-12, JPMorgan's real 2026-07-06 dividend whose previous
session is 2026-07-02 because 2026-07-03 was the observed Independence Day
holiday, the real per-vendor disagreement on KLAC's intraday extremes, and
FRED's real publication lags up to 175 days on GDP.

Synthetic fixtures were considered and rejected. Every one of those facts is
load-bearing for a test, and a fabricated version of it would be *chosen to make
the test pass*, which inverts the relationship the tests exist to have with the
data. The most valuable property of this suite is that several of its assertions
were written before the data was examined and then failed for real reasons —
the missing-trading-day test catching an AAPL gap, the tolerance calibration
failing 79 of 258 bars. Fixtures generated to satisfy the assertions cannot ever
do that again.

The fixtures are loaded through `upsert_dataframe()` — the same write path
production ingestion uses, with the same `DISTINCT ON` and the same conflict
keys — so the loader cannot drift into being a second, more permissive way of
getting rows into `raw`.

**Macro observations are subset to `observation_date >= 2023-01-01`**, which is
the only place fidelity was traded for size. The full table is 49,335 rows,
overwhelmingly the daily Treasury series back to 1962. The subset is 3,073 rows,
and it was verified to preserve every property the macro tests depend on:

- GDP's maximum publication lag is still **175 days** (unchanged);
- all seven point-in-time-capable series still have a first-release date before
  the price window opens, so the ASOF join has something to attach on every
  trading day;
- **18 observations remain whose `first_published_date` falls after their
  `observation_date` and inside the price window** — these are precisely the
  rows that a naive `observation_date` join would leak, and without them
  `assert_point_in_time_macro_differs_from_naive` would pass vacuously.

The three series with no publication history (`DGS10`, `DGS2`, `T10Y2Y`) are
kept in the fixture rather than dropped. Their absence is the point of
`supports_point_in_time_join`, and a fixture containing only the well-behaved
series would quietly delete ADR-0012's most honest column.

### 4. A dbt `WARN` does not fail CI; a `FAIL` or `ERROR` does

The same rule ADR-0005 sets for the Prefect flow, for the same reason.
`assert_dividend_factors_have_a_reference_close` is `severity: warn` on purpose
and legitimately returns a non-zero row count, because corporate actions are
ingested from 2020 while prices cover weeks. CI treating any warning as failure
would fail every build on a condition ADR-0003 explicitly accepted, and the test
would be deleted within a week — which is exactly the outcome that ADR-0005
predicts and exists to prevent.

CI therefore invokes `dbt build` and lets its exit code stand: dbt exits non-zero
on `FAIL`/`ERROR` and zero on `WARN`. Unlike the Prefect flow, CI does **not**
need to parse `run_results.json`, because the flow's problem was that it had to
distinguish "dbt warned" from "dbt never started" in order to *record* a result;
CI has no ledger to write and a dbt that fails to start exits non-zero anyway.

The warning row count is deliberately **not** asserted. It moves legitimately
whenever the fixture window moves, and pinning it would convert a piece of
honest reporting into a brittle test.

## Consequences

**Good.**

- The schema is proven to build from empty on every commit, on Linux, which no
  local run has ever demonstrated.
- The dbt layer — where most of this project's actual assertions live — is under
  CI, with data sharp enough for its non-vacuity guards to fire.
- The Windows/Linux boundary is now tested. It has already paid: this ADR's
  implementation found `test_security_master_scd2.py` hard-coding
  `.venv/Scripts/dbt.exe`, a path that cannot exist on a runner.
- The local/CI test boundary is declared in `pyproject.toml` and greppable,
  rather than being an emergent property of which environment variables happen
  to be set.

**Bad, and accepted.**

- **Real vendor rows are committed to the repository.** ~250 KB of daily OHLCV,
  corporate actions, and FRED observations. FRED is US government data and
  unrestricted; corporate actions are public record; the OHLCV extract is small,
  historical, and non-substitutive for a market data subscription. This is
  judged acceptable, and it is recorded here rather than left implicit so that a
  future decision to grow the fixture is made with the question in view.
- **The fixtures will go stale.** They are a snapshot of 2026-08-05 and nothing
  refreshes them. When the price window moves, `scripts/export_ci_fixtures.py`
  regenerates them from a live warehouse in one command. Staleness is visible
  rather than silent: the fixtures carry fixed dates and the tests assert
  against those dates.
- **CI does not test the vendor adapters against the vendors.** A breaking change
  to Polygon's response shape will be caught by the nightly Prefect flow failing,
  not by CI. That is the correct division: CI tests *this* repository, and a
  vendor changing its API is not a property of a commit.

**Neutral.**

- Runtime is roughly 4–6 minutes, dominated by `dbt build` and the pip install.
  No caching beyond `actions/setup-python`'s pip cache; if this becomes painful
  the fix is a dependency cache, not a reduction in scope.

## Alternatives Considered

**Unit tests only.** The cheapest CI, and it would run in forty seconds. Rejected
because it would cover 57 of this project's ~160 assertions and none of the ones
that encode an ADR. The adjusted-price maths, the merge priority, the
point-in-time macro join and the API's resolution contract would all be outside
CI, and those are the parts of the project a reviewer would actually interrogate.

**A live database shared with development.** Point CI at a persistent Postgres
instead of a per-run service container. Rejected: it destroys the from-empty
migration guarantee, makes runs order-dependent, and lets one bad run poison
every subsequent one.

**Record vendor responses and replay them in CI** (`responses` cassettes for the
Polygon adjusted-close endpoint), so `test_split_reconciliation` could run
everywhere. Rejected because it would silently change what that test *is*. Its
value is being an **external oracle** — an independent party computing the same
number from the same event. A frozen recording of that party's answer is no
longer independent; it is our own assertion with extra steps, and it could never
catch a vendor restatement, which is one of the things the test is for.

**Making CI parse `run_results.json` like the Prefect flow does.** Rejected as
unnecessary duplication: the flow parses it because it must record a status and
must distinguish a warning from a dbt that died before writing the artifact. CI
records nothing and a dead dbt exits non-zero on its own. Adding the parser here
would be a second implementation of ADR-0005's rule with no second reason to
exist.
