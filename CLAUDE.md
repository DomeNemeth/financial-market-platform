# Financial Market Platform

Multi-source financial market data ingestion, warehousing, and API. Portfolio project #1 of a 25-project curriculum; the author is a GTM Engineer moving toward Data/AI Engineering. **Portfolio quality is the goal** — favour rigour, honest documentation, and decisions that can be defended in an interview over speed.

Python 3.11 · FastAPI · SQLAlchemy · Postgres 16 · dbt-postgres 1.11 · Docker Compose · pytest · Polygon.io · OpenFIGI

---

## ⚠️ Environment gotchas — read before running anything

These are deliberate workarounds, not accidents. Don't "fix" them.

**Postgres is on host port 5433, not 5432.** A native Windows PostgreSQL 18 service (`postgresql-x64-18`, bundled with pgAdmin 4) permanently owns 5432 and is intentionally kept running. The project's container publishes on **5433**; container-internal is still 5432.

- `.env` holds *host-side* values (`localhost` / `5433`) used by pytest and dbt.
- `docker-compose.yml` overrides **both** `POSTGRES_HOST` and `POSTGRES_PORT` for the `app` service (`postgres` / `5432`), since containers reach each other by service name.
- If a host-side client fails auth as `market_user`, it is hitting **PG 18**, not our container.

**Always use the venv.** `python` on PATH is 3.14 and has none of the project's dependencies. Use `.venv\Scripts\python.exe`. The venv itself is **3.11.9** — see ADR-0010 for why that is not negotiable.

**Never call `dbt` directly — use `.\scripts\dbt.ps1`.** dbt's `env_var()` reads real OS environment variables, *not* `.env` files. The wrapper loads `.env` via the dotenv CLI and passes `--project-dir`. `~/.dbt/profiles.yml` is kept verbatim identical to the committed `dbt/profiles.yml.example`.
*Known papercut:* the wrapper always appends `--project-dir`, which `dbt --version` rejects. Use `.\scripts\dbt.ps1 debug` to check the install.

**`make` is not installed** (Chocolatey needs admin, deferred). The `Makefile` exists but is not usable. Use raw commands.

**Avast intercepts TLS.** Avast Web/Mail Shield MITMs HTTPS with its own root CA. That root is in the Windows trust store, so browsers work, but anything verifying against a *bundled* CA set fails.

- **At runtime:** `src/common/tls.py` calls `truststore.inject_into_ssl()`. Any new entrypoint making outbound HTTPS must call `enable_system_trust_store()`. Verification stays **on** — never "fix" this with `verify=False`.
- **At install time:** pip in a fresh venv fails with `CERTIFICATE_VERIFY_FAILED`. Fixed permanently by exporting the Windows root store to `~/.certs/windows-root-ca.pem` and running `pip config set --user global.cert <that path>`. Already done on this machine; regenerate if the trust store changes. `winget` and `uv` both hang outright and are unusable here — see ADR-0010.

---

## Commands

```powershell
docker compose up -d                                    # start stack (Postgres + API)
.venv\Scripts\python.exe -m src.common.migrate          # apply pending schema migrations
.venv\Scripts\python.exe -m src.common.migrate --status # show migration state

.venv\Scripts\python.exe -m pytest -m "not integration" # 23 unit tests, no network/DB
.venv\Scripts\python.exe -m pytest tests/integration -q # 27 tests, needs stack + API key

.venv\Scripts\python.exe -m ruff check src/ tests/ scripts/

.\scripts\dbt.ps1 debug                                 # verify dbt connection
.\scripts\dbt.ps1 build                                 # seed + snapshot + run + test (71 nodes, 1 expected WARN)

# ingestion
.venv\Scripts\python.exe -m src.ingestion --tickers AAPL MSFT --start 2026-06-01 --end 2026-06-30
.venv\Scripts\python.exe -m src.ingestion.security_master --tickers AAPL NVDA
.venv\Scripts\python.exe -m src.ingestion.corporate_actions --tickers NVDA --since 2024-01-01

curl http://localhost:8000/health                       # -> {"status":"ok","db":"connected"}
```

---

## Where we are — Phase 3 of 7, complete

**Phases 1 and 2 are complete. Phase 3 (dbt intermediate → marts, adjusted prices in SQL) is complete and tagged `v0.3`.** Phase 4 (FastAPI point-in-time prices endpoint) is next; it serves from `marts.fct_security_price_daily`.

Everything below was verified against real data and a live database on 2026-08-02, not against fixtures.

| Check | Status |
|---|---|
| `ruff check src/ tests/ scripts/` | ✅ clean |
| `pytest -m "not integration"` | ✅ 23 passed (no network, no DB) |
| `pytest tests/integration` | ✅ 48 passed |
| `dbt build` | ✅ 71 nodes, PASS=70 WARN=1 ERROR=0 (61 data tests) |
| Migrations | ✅ 0001–0004 applied |
| Price data | ✅ 6 tickers × 43 contiguous sessions (2026-06-01 → 2026-07-31) |
| `/health` | ✅ `{"status":"ok","db":"connected"}` |

The one WARN is `assert_dividend_factors_have_a_reference_close` at 142 rows, and it is **expected** — see "Things that earned their keep" below. A build with zero warnings would mean that test had been silently weakened.

### What Phase 2 added

**Migration runner** (`migrations/` + `src/common/migrate.py`). `docker/postgres/init.sql` only executes on an *empty* data directory, so it can never reach an existing database — the Phase 1 `volume BIGINT → NUMERIC` change had to be hand-applied. Migrations are numbered, forward-only, and checksummed; a migration edited after being applied fails loudly rather than silently diverging. Checksums normalise line endings so a Windows checkout and a Linux one agree. `init.sql` is now bootstrap-only (extensions + schemas).

**Security master** (`raw.security_identity`, `raw.security_master`, `src/ingestion/security_master.py`). `security_id` is a durable surrogate anchored on **FIGI**, not ticker. Securities without a FIGI get a *provisional* `vendor_ticker:` identity that is promoted in place when OpenFIGI resolves — `identity_key` changes, `security_id` does not, so no foreign key is ever invalidated. Verified: NVDA → `BBG000BBJQV0`, AAPL → `BBG000B9XRY4`, with real list dates (AAPL 1980-12-12). CUSIP/ISIN columns exist and are **NULL by design**.

**SCD2 snapshot** (`dbt/snapshots/security_master_snapshot.sql`). `strategy='check'` over the mutable attribute set. Gives the *system-time* axis; valid time lives in `list_date`/`delist_date`. Both are tested.

**Corporate actions** (`raw.corporate_actions`, `src/ingestion/corporate_actions.py`). Splits and cash dividends keyed on `ex_date`. CHECK constraints reject a split with no ratio or a dividend with no amount — both would otherwise vanish silently from the factor product. Real data ingested: NVDA's 2021-07-20 4:1 and 2024-06-10 10:1, plus KLAC's 2026-06-12 10:1.

**Trading calendar** (`src/common/calendar.py`, `dbt/seeds/trading_calendar.csv`, 3,162 sessions). Replaces date arithmetic, which is wrong in ways that don't announce themselves: `ex_date - 1 day` lands on a weekend a fifth of the time, and ADR-0003's dividend factor needs the previous *session*.

**Adjusted prices** (`src/transforms/adjusted_prices.py`). The pure-Python reference implementation ADR-0003 calls for — it did not previously exist despite being claimed. Decimal throughout, never float. Two named series (`split_adjusted_*`, `total_return_adjusted_*`); nothing is called `adjusted_close`.

**Range-based ingestion.** `--start/--end` on the CLI. One API call covers a whole range, which matters on a 5-req/min tier. Every ticker's observed dates are diffed against the exchange calendar and gaps reported.

### Things that earned their keep

- **The missing-trading-day dbt test failed on its first run**, correctly flagging that AAPL had a lone 2026-07-29 bar with all of July absent behind it. Backfilled to close the gap; it now passes over a contiguous dataset. It is demonstrably non-vacuous.
- **The split reconciliation matches Polygon's own adjusted close to 1e-6 relative** on a real 10:1 split, with explicit guards that the window straddles the split and that the raw series really does contain the ~-90% artefact being corrected.
- **Idempotency proven on real volume**: re-running the 105-row backfill left `count(*)` and `sum(id)` identical (no new IDs allocated) with `ingested_at` advanced.

- **Idempotency is now a regression test, not just a manual observation** (`tests/integration/test_idempotency.py`). Covers duplicates *across* loads (silent row duplication) and *within* one batch (Postgres `CardinalityViolation` aborting the whole load). The intra-batch case was confirmed non-vacuous by running the same upsert without `DISTINCT ON` and watching Postgres reject it.
- **Partial-batch failure asserts both halves of ADR-0011**: successful tickers committed *and* the run recorded `FAILED`. Includes a clean-batch test so an implementation that failed every run couldn't pass, and a retry test proving re-running after a fix adds only the missing ticker.

### What Phase 3 added

**Intermediate layer.** `int_prices_with_calendar` (identity resolution + calendar check), `int_corporate_actions__factors` (per-event factors), `int_prices_with_adjustments` (the ADR-0003 maths). All views.

**Identity resolution has a home.** `raw.prices` is keyed on ticker and carries **no `security_id`** — price ingestion predates the security master. The ticker → `security_id` resolution happens in `int_prices_with_calendar`, bounded by the security's **valid-time window** (`list_date`/`delist_date`), never by bare ticker equality. Two tests bracket it: one fails on a bar resolving to *no* security, one on a bar resolving to *several*.

**The cumulative product in SQL.** Postgres has no `PRODUCT()`, so factors are `exp(sum(ln(...)))`, computed as `exp(total_ln − running_ln)` — subtract in log space, exponentiate once. That form makes the latest bar's factor **exactly** 1 (`exp(0::numeric) = 1`), so ADR-0003's "the latest bar equals the raw bar" holds by construction rather than by rounding. Three failure modes measured and documented in the **ADR-0003 addendum** — read it before touching this maths.

**Marts.** `dim_security` (current-state, both time axes under unambiguous names) and `fct_security_price_daily` (raw OHLCV + factors + both adjusted series + `actions_observed_through` as ADR-0003's `as_of`). The fact joins the dimension on `security_id` **and** the valid-time window: `security_id` alone can never fail, because the surrogate is durable and never reused, so an unbounded join attaches reference data from the wrong period and nothing raises.

**Data backfilled to make Phase 3 checkable.** KLAC had the 10:1 split but no price bars, and JPM/MSFT/V had bars but no security master row. Both were ingested. The dataset now has a split, two in-window dividends, and three no-action control securities.

### Things that earned their keep

- **The reconciliation is now three-way**, not two. SQL vs Python over *identical staged bars* (so only the arithmetic can differ), plus each against Polygon. Verified non-vacuous by flipping `f.ex_date <= b.trading_date` to `<` — the classic off-by-one — which failed 8 tests across both legs.
- **The monotonicity test was verified the same way**: inverting the log subtraction (`running − total` instead of `total − running`) fails it. The step-function test correctly stays green on that mutation, which is exactly why both exist — one catches direction, the other catches drift.
- **`assert_dividend_factors_have_a_reference_close` is `severity: warn` on purpose.** 142 rows, and that is correct: actions are ingested from 2020 while prices cover weeks, so most historical dividends have no bar behind them, and ADR-0003 skips those. Erroring would fail every build on a condition the ADR already accepted, and the test would be deleted within a week. The count should only shrink as history is backfilled.
- **The total-return series has no external oracle** — Polygon's adjusted aggregates are split-only. It is pinned instead by a definitional property: on the session before an ex-date, `total_return_adjusted_close` equals `close − dividend`, exactly. Verified at 214.75 → 214.50 (NVDA) and 334.47 → 332.97 (JPM).
- **JPM's 2026-07-06 ex-date is in the test suite specifically** because its previous session is 2026-07-02 — 2026-07-03 is the observed Independence Day holiday. `ex_date - 1 day` lands on Sunday the 5th, finds no bar, and silently drops the dividend. It is the live instance of the hazard the trading calendar exists for.

### Known issues

- **No CI.** `.github/workflows/` is an empty directory. CLAUDE.md previously implied CI existed ("CI needs no wrapper"); it does not.
- The provisional→FIGI merge edge case (two identities resolving to one FIGI) is **detected but not repaired** — see ADR-0004. Deliberate.
- Polygon's free tier caps aggregates at **2 years**; requests for older bars 403. Reference endpoints (splits, dividends, ticker details) are not capped. This is why the split reconciliation uses KLAC 2026 rather than NVDA 2024.
- `exchange_calendars` only generates ~1 year forward; the seed is clamped to 2027-08-02 and must be regenerated periodically.

---

## Architecture decisions — do not silently revise

Full rationale in `docs/adr/`. **ADRs 0001, 0002, 0003 (+ its SQL addendum), 0004, 0007, 0008, 0010, 0011 are written.** ADRs **0005** (Prefect), **0006** (source priority), and **0009** (API design) are still template stubs — treat their subject matter as undecided.

**ADR-0006 is the one that will bite first.** The moment a second vendor lands, two sources will disagree about a price on the same `(security_id, trading_date)` and something has to decide which wins. The intermediate layer is already the place ADR-0008 reserves for that decision, and it is currently empty of it — `int_prices_with_calendar` reads one source and does not merge. Whoever adds the second adapter should write ADR-0006 *before* the merge logic, not after.

- **Postgres is the transform substrate** (ADR-0001). Python ingestion writes Parquet as an immutable archive **and** loads the same rows into `raw`. dbt runs entirely against Postgres. DuckDB is deliberately **not** in the critical path.
- **Parquet is written before Postgres** (ADR-0002), so a crash leaves an archived file with no row — recoverable — rather than a row with no provenance.
- **`security_id`, never ticker, is the join key** (ADR-0004, ADR-0007). Tickers are leased and reused; joining on them splices unrelated companies together silently.
- **CUSIP/ISIN are licensed.** Columns exist, populated only from licence-free sources. **Never** fabricate them — a checksum-valid fake is worse than NULL.
- **Two adjusted series, never one** (ADR-0003). `split_adjusted_*` for charting, `total_return_adjusted_*` for returns. Factors are stored rather than adjusted prices, so a new action doesn't rewrite history.
- **Cumulative products are `exp(sum(ln(...)))`, in `numeric`, never `float8`** (ADR-0003 addendum). Every log sum is `coalesce(..., 0)` — without it, a security with *no* corporate actions gets `sum()` over zero rows = NULL and its entire adjusted series becomes NULL. That is the majority case, not an edge case.
- **A non-positive dividend factor is NULL, never clamped** (ADR-0003 addendum). A dividend at or above the previous close can't enter a log product. NULL confines the damage to the affected bars and `assert_dividend_factors_are_positive` names the cause; a clamped value would be wrong by the size of the dividend and look entirely plausible.
- **Facts join dimensions on `security_id` AND the valid-time window.** `security_id` alone always "works" — the surrogate is durable and never reused — so it fails silently by attaching reference data from the wrong period. Valid time is `list_date`/`delist_date`; the snapshot's `dbt_valid_from`/`to` is *system* time and answers a different question.
- **Adjusted-price logic is implemented twice on purpose**: pure Python in `src/transforms/adjusted_prices.py` as a unit-testable reference, dbt SQL for the pipeline. A targeted exception to "transform in dbt", not a pattern.
- **Per-source staging models** (`stg_polygon__prices`, never a merged `stg_prices`). Cross-source merging happens at the *intermediate* layer (ADR-0008).
- **Raw stays raw.** Polygon is fetched with `adjusted=false` so adjustment stays ours and auditable.
- **Collect-and-continue on partial batch failure** (ADR-0011), then fail the run inside the ledger. Committed work survives; no incomplete batch is ever recorded `SUCCESS`.
- **dbt and Python are pinned to stable ranges** (ADR-0010). Not upgradeable casually — read that ADR first.
- **`docker-compose.yml` belongs at repo root.** `docker/` holds *inputs* to compose.

---

## Conventions

- **Adapters** subclass `BaseAdapter`, set `SOURCE_NAME`, implement `fetch()` and `validate()`. `fetch()` raises on HTTP errors but returns an *empty DataFrame* for legitimately empty results.
- **All Postgres writes go through `upsert_dataframe()`** (`src/common/database.py`), so the idempotency guarantee is implemented once. It stages into a session-scoped `TEMP` table with `ON COMMIT DROP` — never a permanent one. Built via `CREATE TEMP TABLE ... AS SELECT <cols> FROM <target> WITH NO DATA`, **not** `LIKE`: `LIKE` copies `NOT NULL` but not defaults, so a `BIGSERIAL` id would arrive as a NOT NULL bigint with no sequence and every insert would fail.
- **`DISTINCT ON` over the conflict key is required, not defensive.** Postgres raises *"ON CONFLICT DO UPDATE command cannot affect row a second time"* if one statement presents two rows with the same key, and vendors do return intra-batch duplicates.
- **pandas sentinels must become `None` before psycopg2 sees them.** `pd.NA` raises; `float('nan')` is adapted to a literal Postgres `NaN` that `NUMERIC` silently accepts — a missing `vwap` would land as NaN, not NULL, and poison every aggregate. `to_records()` handles this.
- **Every pipeline run goes through `RunLedger`**, which never swallows exceptions.
- **Timestamps:** Polygon daily bars are midnight UTC of the trading date. Use the UTC date directly; converting to ET shifts the date back a day. Regression test exists.
- **Volume is fractional, not integral.** `raw.prices.volume` is `NUMERIC(20,6)`; `round(volume)::bigint` happens in staging. Regression test exists.
- **Money is `Decimal`, never float.** Adjustment factors multiply, so float error compounds along the chain.
- **API keys go in headers, never query params.** `requests` embeds the full URL in exception messages, which get persisted to `pipeline_runs.error_message` — a `?apiKey=` leaks the credential into the database.
- **SQLAlchemy `text()` will not bind a `:param` immediately followed by a colon.** Use `CAST(:param AS jsonb)`, never `:param::jsonb`. This caused a real bug.
- **Schema changes go in `migrations/`, never in `init.sql`.** Never edit an applied migration — the checksum will reject it. Add a new one.
- **`tests/unit/` must run with Docker down.** Anything needing a database, network, or API key goes in `tests/integration/` with `pytestmark = pytest.mark.integration`. Synthetic fixtures use a distinct `source` value or a `ZZ`-prefixed ticker and clean up after themselves, so they can never be mistaken for real ingested data.
- **A test that only ever passes proves little.** Each significant test carries a non-vacuity guard — that the window straddles the event, that the raw artefact is really present, that a clean batch still succeeds.
- Parquet path: `data/raw/prices/{source}/{ticker}/{YYYY-MM-DD}.parquet` (gitignored).
- Commit at task boundaries; the author uses commits as session checkpoints.

---

## Roadmap

1. ✅ **Foundation** — Docker, Postgres, run ledger, Polygon adapter
2. ✅ **Reference data** — security master, corporate actions, trading calendar → tagged `v0.2`
3. ✅ **Transform layer** — dbt intermediate → marts, adjusted prices in SQL → tagged `v0.3`
4. API layer — FastAPI + point-in-time prices endpoint → tag `v0.5` ← *next*
5. Completeness — remaining adapters (Yahoo, Alpha Vantage, FRED), Prefect orchestration
6. Polish — Streamlit dashboard, CI/CD
7. Release — docs, ADRs, clean-environment test → tag `v1.0`

Realistic timeline: 10–14 weeks part-time.
