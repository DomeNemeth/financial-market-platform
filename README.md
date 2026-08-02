# Financial Market Platform

A point-in-time-correct market data warehouse: ingests daily equity bars and
reference data from multiple vendors, reconciles them, and serves them over an
API.

The interesting problem here is not moving rows. It is that market data lies to
you in ways that produce **plausible wrong answers rather than errors** — and a
pipeline that fails loudly is strictly better than one that returns a confident
number nobody can reproduce.

Three concrete examples, all of which this project handles explicitly:

| Trap | What naïvely happens | What it costs |
|---|---|---|
| **Tickers are reused** | `FB` → `META` fragments one company's history; a delisted symbol reassigned to an unrelated company splices two histories into one | A backtest silently runs on a chimera |
| **Corporate actions** | NVDA closed at \$1,208.88 on 2024-06-07 and ~\$120 the next session after a 10:1 split | A −90% return that never happened |
| **Vendors restate** | Today's reference data gets applied to yesterday's prices | Look-ahead bias — the number looks fine and is wrong |

> **Status: Phase 2 of 7.** The ingestion and reference-data layers are complete
> and tested against live vendor data. **There is no data API yet** — only a
> health endpoint. See [Honest status](#honest-status) before evaluating this.

---

## Architecture

```
   Polygon.io ─┐
    OpenFIGI ──┼──▶  Python ingestion  ──┬──▶  Parquet archive   (immutable, append-only)
  (future:     │     + run ledger        │
   Yahoo,      │                         └──▶  Postgres `raw`    (working copy, upserted)
   FRED, AV) ──┘                                      │
                                                      ▼
                                              dbt: staging ──▶ intermediate ──▶ marts
                                                      │
                                                      ▼
                                              FastAPI  (Phase 4)
```

Every batch is written to **Parquet first, Postgres second**. A crash between
them leaves an archived file with no database row — recoverable and detectable —
rather than a row whose provenance was never captured. Postgres is disposable and
rebuildable from the archive; the archive is not rebuildable from the vendor,
because deep history is rate-limited and paid.

**Postgres is the only transform substrate.** DuckDB is deliberately *not* in the
critical path — it stays available for ad-hoc querying of the Parquet tree.
[ADR-0001](docs/adr/0001-warehouse-architecture.md) covers why, including why
DuckDB's single-writer model rules it out here.

### The data model

| Table | Grain | Purpose |
|---|---|---|
| `raw.prices` | (ticker, trading_date, source) | OHLCV daily bars, unadjusted |
| `raw.security_identity` | security_id | Durable surrogate key, anchored on FIGI |
| `raw.security_master` | (security_id, source) | Current vendor reference snapshot |
| `raw.corporate_actions` | (security_id, action_type, ex_date, source) | Splits and cash dividends |
| `public.pipeline_runs` | run | Every run, success or failure |
| `intermediate.int_prices_with_calendar` | (security_id, trading_date) | Bars resolved to a security_id, checked against the session calendar |
| `intermediate.int_corporate_actions__factors` | (security_id, ex_date) | Per-event split and dividend factors |
| `intermediate.int_prices_with_adjustments` | (security_id, trading_date) | The ADR-0003 adjustment maths |
| `marts.dim_security` | security_id | Current-state dimension, both time axes exposed |
| `marts.fct_security_price_daily` | (security_id, trading_date) | Raw OHLCV + factors + both adjusted series |

`security_id` — not the ticker — is the join key everywhere. Tickers are *leased*
by exchanges and get reassigned, so joining on them is silently incorrect.
Identity is anchored on **FIGI** (free and redistributable via OpenFIGI); a
security seen before its FIGI resolves gets a *provisional* identity that is later
promoted **in place**, so no foreign key is ever invalidated.

CUSIP and ISIN columns exist and are **always NULL** in this deployment. They are
licensed identifiers, and a checksum-valid fake is worse than an empty column
because it looks usable. See
[ADR-0007](docs/adr/0007-identifier-strategy.md).

---

## Quickstart

**Prerequisites:** Docker Desktop, Python 3.11, and a free
[Polygon.io](https://polygon.io) API key. An
[OpenFIGI](https://www.openfigi.com/api) key is optional but raises the rate
limit from 25 to 250 requests/minute.

```bash
git clone <repo> && cd financial-market-platform
cp .env.example .env          # then add your POLYGON_API_KEY

python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"           # macOS / Linux

docker compose up -d
.venv/Scripts/python.exe -m src.common.migrate          # create the schema

curl http://localhost:8000/health
# {"status":"ok","db":"connected"}
```

> Postgres publishes on host port **5433**, not 5432, so it coexists with a
> locally-installed Postgres. Change it in `.env` and `docker-compose.yml` if you
> prefer 5432.

### Ingest some data

```bash
PY=.venv/Scripts/python.exe

# Reference data first — corporate actions need a security_id to attach to
$PY -m src.ingestion.security_master   --tickers AAPL MSFT NVDA
$PY -m src.ingestion.corporate_actions --tickers NVDA --since 2020-01-01

# Prices: one API call covers the whole range
$PY -m src.ingestion --tickers AAPL MSFT NVDA --start 2026-06-01 --end 2026-06-30
```

Re-running any of these is safe. Loads are `INSERT ... ON CONFLICT` upserts, so a
repeat run updates in place rather than duplicating — verified by tests, not just
asserted.

### Build the warehouse

```bash
./scripts/dbt.ps1 build     # seed + snapshot + run + test
```

`scripts/dbt.ps1` exists because dbt's `env_var()` reads real OS environment
variables, not `.env`. Call it rather than bare `dbt`. On CI, where variables are
set natively, `dbt` works directly.

---

## Testing

```bash
PY=.venv/Scripts/python.exe

$PY -m pytest -m "not integration"   # 23 unit tests — no network, no database
$PY -m pytest tests/integration      # 27 tests — needs the stack + a Polygon key
./scripts/dbt.ps1 test               # 23 data tests
$PY -m ruff check src/ tests/ scripts/
```

A few of these are worth calling out, because a test suite that only passes is
not evidence of much:

- **The missing-trading-day test failed on its first run** and was right to. It
  found that AAPL had a lone 2026-07-29 bar with all of July missing behind it.
  It compares observed dates against a committed exchange-calendar seed, bounded
  per ticker to its own observed range so it detects *interior* gaps rather than
  flagging every session before ingestion began.
- **Split adjustment is reconciled against Polygon's own adjusted close** to
  1e-6 relative, on a real 10-for-1 split — with explicit guards that the window
  actually straddles the split and that the raw series really does contain the
  ~−90% artefact being corrected. Without those guards the test could pass while
  proving nothing.
- **Intra-batch duplicate handling is verified against the actual failure.**
  Postgres raises `CardinalityViolation` if one statement presents two rows with
  the same conflict key, aborting the whole batch. Vendors do return duplicates
  from overlapping paginated ranges.
- **Partial-batch failure asserts both halves of the policy**: successful tickers
  are committed *and* the run is still recorded `FAILED`. Either one alone is the
  wrong behaviour.

---

## Design decisions

Written up as ADRs, with the alternatives that were rejected and why:

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-warehouse-architecture.md) | Postgres as the transform substrate; DuckDB out of the critical path |
| [0002](docs/adr/0002-parquet-landing-zone.md) | Parquet as an immutable landing zone, written before Postgres |
| [0003](docs/adr/0003-adjusted-price-methodology.md) | Two named adjusted series; store factors, not adjusted prices |
| [0004](docs/adr/0004-bitemporal-security-master.md) | Bitemporal security master — valid time and system time kept separate |
| [0007](docs/adr/0007-identifier-strategy.md) | Surrogate key anchored on FIGI; licensed identifiers never fabricated |
| [0008](docs/adr/0008-dbt-modeling-conventions.md) | Per-source staging models; cross-source merging only at intermediate |
| [0010](docs/adr/0010-dependency-and-runtime-pinning.md) | Stable-only dependency ranges and a pinned interpreter |
| [0011](docs/adr/0011-ingestion-failure-policy.md) | Collect-and-continue on partial failure, then fail the run |

ADRs **0005** (orchestration), **0006** (source priority), and **0009** (API
design) are still stubs — that subject matter is genuinely undecided, and the
files are placeholders rather than documentation.

Two decisions that are load-bearing enough to summarise here:

**Nothing is called `adjusted_close`.** There are two series —
`split_adjusted_*` for charting and price-level comparison, and
`total_return_adjusted_*` for returns. Collapsing them into one ambiguously-named
column is the single most common way adjusted-price data gets misused. The mart
stores raw OHLCV plus the cumulative *factors*, so a new corporate action
recomputes factors instead of rewriting history.

**The adjustment maths is implemented twice on purpose.**
[`src/transforms/adjusted_prices.py`](src/transforms/adjusted_prices.py) is a
pure-Python reference implementation — `Decimal` throughout, readable, directly
unit-testable — and dbt SQL implements it again for the pipeline. Disagreement
between the two is a real signal. This is a deliberate exception to "transform in
dbt", not a pattern.

Both implementations are now live, and
[`tests/integration/test_split_reconciliation.py`](tests/integration/test_split_reconciliation.py)
reconciles them three ways: SQL against Python over identical staged bars, and
each against Polygon's own adjusted series as an external oracle. The SQL side
computes the cumulative product as `exp(sum(ln(...)))`, because Postgres has no
`PRODUCT()` aggregate — a technique with three failure modes, two of which fail
silently. They are measured and documented in the
[addendum to ADR-0003](docs/adr/0003-adjusted-price-methodology.md#addendum-computing-the-factor-products-in-sql).

---

## Project layout

```
migrations/          Numbered, forward-only, checksummed SQL migrations
src/
  common/            config, database, logging, TLS, trading calendar, migrate
  ingestion/         Polygon adapter, security master, corporate actions, run ledger
  transforms/        Pure-Python adjusted-price reference implementation
  api/               FastAPI app (health only so far)
dbt/
  models/staging/       Per-source staging models
  models/intermediate/  Identity resolution, calendar checks, adjustment maths
  models/marts/         dim_security, fct_security_price_daily
  snapshots/         SCD2 security master history
  seeds/             Committed trading calendar
  tests/             Singular data tests
docs/adr/            Architecture decision records
tests/unit/          No network, no database
tests/integration/   Needs the stack and a live API key
```

Schema changes go in `migrations/`, never in `docker/postgres/init.sql` — the
Postgres entrypoint only runs `init.sql` when the data directory is empty, so
editing it cannot reach a database that already exists. Applied migrations are
immutable; the runner rejects one whose checksum has changed.

---

## Honest status

**Working and verified against live vendor data:**
Polygon price ingestion (range-based, idempotent, rate-limit aware) · Parquet
archive · security master with real OpenFIGI resolution · corporate actions
(splits and dividends) · SCD2 snapshot · trading calendar · migration runner ·
run ledger · full dbt DAG from staging through intermediate to marts, with both
adjusted series reconciled against the Python reference and against Polygon ·
61 dbt data tests · 71 Python tests · health endpoint.

**Not built yet:**

- **No data API.** `/health` is the only endpoint. The point-in-time prices
  endpoint is Phase 4 — `fct_security_price_daily` is the table it will serve
  from.
- **One vendor.** Yahoo, Alpha Vantage, and FRED adapters are Phase 5, which is
  also when the per-source staging convention starts paying for itself.
- **No orchestration.** Ingestion is CLI-driven; Prefect is Phase 5.
- **No CI.** `.github/workflows/` is empty.

**Known limitations:**

- Only splits and cash dividends are handled. Spin-offs, mergers, rights issues,
  and return-of-capital are **not**, and would produce a wrong series if present.
  A stated scope limit, not an oversight.
- Polygon's free tier caps aggregates at two years; reference endpoints are
  uncapped. This is why the split reconciliation uses a 2026 event rather than
  NVDA's canonical 2024 split.
- If a provisional identity later resolves to a FIGI that already exists under a
  different `security_id`, the two rows need merging. This is **detected but not
  repaired** — see [ADR-0004](docs/adr/0004-bitemporal-security-master.md).
- The Parquet archive's immutability is a convention enforced by a single writer,
  not by filesystem permissions. No archive-to-`raw` reconciliation check exists yet.
- The total-return series has **no external oracle**. Polygon's adjusted
  aggregates are split-only, so the dividend leg is checked against the Python
  reference and against a definitional property (on the session before an
  ex-date, the total-return close equals close minus the dividend) rather than
  against a third party.
- `assert_dividend_factors_have_a_reference_close` warns on 142 rows and is
  *expected* to. Corporate actions are ingested from 2020 while prices cover
  weeks, so most historical dividends have no bar behind them; ADR-0003 skips
  those with no factor applied. The count should only ever shrink as price
  history is backfilled.

---

## Roadmap

1. ✅ Foundation — Docker, Postgres, run ledger, Polygon adapter
2. ✅ Reference data — security master, corporate actions, trading calendar
3. ✅ Transform layer — dbt intermediate → marts, adjusted prices in SQL
4. ⬜ API layer — point-in-time prices endpoint → `v0.5`
5. ⬜ Completeness — remaining adapters, Prefect orchestration
6. ⬜ Polish — Streamlit dashboard, CI/CD
7. ⬜ Release — docs, clean-environment test → `v1.0`

---

## Context

Portfolio project #1 of a 25-project curriculum, built by a GTM Engineer moving
toward Data/AI Engineering. The goal is work that survives scrutiny: decisions
that can be defended, claims that are true, and tests that would actually catch
the failure they describe.

Licensed under the [MIT License](LICENSE).
