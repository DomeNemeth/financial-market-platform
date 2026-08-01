-- 0004_drop_prices_tmp
-- Remove the leftover staging table from the pre-fix load path.
--
-- BaseAdapter.load_to_postgres() used to `df.to_sql("prices_tmp", schema="raw",
-- if_exists="replace")`, which creates a *permanent* table in the raw schema.
-- It has since been replaced by a session-scoped TEMP table with ON COMMIT DROP.
-- This drops the artifact that path left sitting in `raw`, where dbt introspects
-- sources and where a stale copy of price data has no business being.

DROP TABLE IF EXISTS raw.prices_tmp;
