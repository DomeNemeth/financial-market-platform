# Financial Market Platform

Multi-source financial market data ingestion, warehousing, and API. Portfolio project #1 of a 25-project curriculum; the author is a GTM Engineer moving toward Data/AI Engineering. **Portfolio quality is the goal** — favour rigour, honest documentation, and decisions that can be defended in an interview over speed.

Python 3.11 · FastAPI · SQLAlchemy · Postgres 16 · dbt-postgres · Docker Compose · pytest · Polygon.io

---

## ⚠️ Environment gotchas — read before running anything

These are deliberate workarounds, not accidents. Don't "fix" them.

**Postgres is on host port 5433, not 5432.** A native Windows PostgreSQL 18 service (`postgresql-x64-18`, bundled with pgAdmin 4) permanently owns 5432 and is intentionally kept running. The project's container publishes on **5433**; container-internal is still 5432.

- `.env` holds *host-side* values (`localhost` / `5433`) used by pytest and dbt.
- `docker-compose.yml` overrides **both** `POSTGRES_HOST` and `POSTGRES_PORT` for the `app` service (`postgres` / `5432`), since containers reach each other by service name.
- If a host-side client fails auth as `market_user`, it is hitting **PG 18**, not our container.

**Always use the venv.** `python` on PATH is 3.14 and has none of the project's dependencies. Use `.venv\Scripts\python.exe`.

**Never call `dbt` directly — use `.\scripts\dbt.ps1`.** dbt's `env_var()` reads real OS environment variables, *not* `.env` files. The wrapper loads `.env` via the dotenv CLI and passes `--project-dir`. `~/.dbt/profiles.yml` is kept verbatim identical to the committed `dbt/profiles.yml.example`.

**`make` is not installed** (Chocolatey needs admin, deferred). The `Makefile` exists but is not usable — `scripts/dbt.ps1` fills part of that gap. Use raw commands.

---

## Commands

```powershell
docker compose up -d                              # start stack (Postgres + API)
.venv\Scripts\python.exe -m pytest tests/unit -v   # unit tests
.\scripts\dbt.ps1 debug                           # verify dbt connection
.\scripts\dbt.ps1 run                             # build models
.\scripts\dbt.ps1 test                            # run data tests
curl http://localhost:8000/health                 # -> {"status":"ok","db":"connected"}
```

---

## Where we are — Phase 1 of 7

Last session (2026-08-01) fixed the port conflict, unblocked the test suite, and wired up dbt. Commits `b3ae9fc`, `187c339`, `249f3f9` on `main`. Working tree clean, **not pushed** to `origin`.

Phase 1 checklist:

| # | Item | Status |
|---|------|--------|
| 1 | Schemas exist (`raw`/`staging`/`intermediate`/`marts`) | ✅ |
| 2 | Health endpoint returns OK | ✅ |
| 3 | Unit tests pass | ✅ 10/10 |
| 4 | Ingest one ticker for a known trading day | ⬜ **next** |
| 5 | Parquet file written | ⬜ |
| 6 | Row lands in `raw.prices` | ⬜ |
| 7 | Run ledger records it | ⚠️ verified by tests, not by a real run |
| 8 | dbt staging view builds + tests pass | ✅ *but see below* |

**`raw.prices` is empty (0 rows).** The 5 dbt tests currently pass **vacuously** — `not_null` on an empty table always passes. The plumbing is proven; the data is not.

### Next steps

1. **Run the first real Polygon ingestion.** Start with `--tickers AAPL` and a known-good trading day (a Tuesday/Wednesday, not a US holiday) so an empty result means a bug, not a closed market. Free tier is 5 req/min; the CLI already sleeps 12s between tickers.
2. Verify the Parquet file, the `raw.prices` row, and the `pipeline_runs` entry — checklist items 4–7.
3. Re-run `.\scripts\dbt.ps1 test` now that rows exist. This is the run that makes item 8 meaningful.
4. **Resolve the dbt beta** (see below) before tagging.
5. Tag `v0.1`, then Phase 2.

### Known issues

- **`dbt-core` is `1.12.0b3` — a pre-release.** Cause: `pyproject.toml` pins `dbt-postgres>=1.7`, unbounded. A beta is a portfolio risk. Note the interaction: `_staging.yml` nests `accepted_values` under `arguments:` (current-dbt syntax, added to clear a deprecation warning) — pinning backward to an older stable may require reverting that line.
- `tests/unit/test_run_ledger.py` genuinely requires a live database, so it is an integration test living in `tests/unit/`. Consider moving to `tests/integration/`.
- `README.md` is a single line. Needs writing before `v1.0`.

---

## Architecture decisions — do not silently revise

Reviewed and scoped across 7 phases before any code was written. Full rationale in `docs/adr/` (ADRs 0001–0009).

- **Postgres is the transform substrate.** Python ingestion writes Parquet as an immutable archive **and** loads the same rows into the `raw` Postgres schema. dbt runs entirely against Postgres. DuckDB is a standalone tool for querying Parquet, deliberately **not** in the critical path.
- **Per-source staging models** (`stg_polygon__prices`, never a merged `stg_prices`). Cross-source merging happens at the *intermediate* layer.
- **CUSIP/ISIN are licensed identifiers.** The columns exist in the schema but are populated only via free OpenFIGI lookups. Never fabricate them.
- **Adjusted-price logic is implemented twice on purpose:** pure Python in `src/transforms/adjusted_prices.py` as a unit-testable reference, and dbt SQL for the actual pipeline. Not via dbt-python models.
- **Raw prices stay raw.** The Polygon adapter requests `adjusted=false` so adjustment logic remains ours and auditable.
- **`docker-compose.yml` belongs at repo root**, not in `docker/`. Compose discovers it in the working directory, and `build.context: .` resolves relative to it. `docker/` holds *inputs* to compose (`Dockerfile.api`, `postgres/init.sql`).

---

## Conventions

- **Adapters** subclass `BaseAdapter` (`src/ingestion/adapters/base.py`), set `SOURCE_NAME`, and implement `fetch()` and `validate()`. `write_parquet()` and `load_to_postgres()` are shared. `fetch()` raises on HTTP errors but returns an *empty DataFrame* for legitimately empty results (weekends, holidays).
- **Every pipeline run goes through `RunLedger`** (`src/ingestion/run_ledger.py`), a context manager writing to `public.pipeline_runs`. It never swallows exceptions.
- **Loads are idempotent** — `INSERT ... ON CONFLICT` on `(ticker, trading_date, source)`. Re-running an ingestion must not duplicate rows.
- **Timestamps:** Polygon daily bars are midnight UTC of the trading date. Use the UTC date directly; converting to ET shifts the date back a day. There is a regression test for this.
- **SQLAlchemy `text()` will not bind a `:param` immediately followed by a colon** — it protects Postgres `::` casts. Use `CAST(:param AS jsonb)`, never `:param::jsonb`. This caused a real bug.
- Parquet path: `data/raw/prices/{source}/{ticker}/{YYYY-MM-DD}.parquet` (gitignored).
- Commit at task boundaries; the author uses commits as session checkpoints.

---

## Roadmap

1. **Foundation** — Docker, Postgres, run ledger, Polygon adapter → tag `v0.1` ← *here*
2. Reference data — security master, corporate actions, trading calendar
3. Transform layer — dbt staging → intermediate → marts, adjusted prices
4. API layer — FastAPI + point-in-time prices endpoint → tag `v0.5`
5. Completeness — remaining adapters (Yahoo, Alpha Vantage, FRED), Prefect orchestration
6. Polish — Streamlit dashboard, CI/CD
7. Release — docs, ADRs, clean-environment test → tag `v1.0`

Realistic timeline: 10–14 weeks part-time.
