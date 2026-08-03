-- Fails if any macro observation stored a NUMERIC NaN.
--
-- Postgres NUMERIC accepts 'NaN' silently. It is not a constraint violation, it
-- is not NULL, and it survives every not_null test in this project — but it
-- poisons every aggregate computed over the column, turning an average, a
-- correlation or a moving window into NaN with nothing raising anywhere.
--
-- The specific path that produces one is already documented as a convention in
-- CLAUDE.md and cost a real bug on the price side: pandas sentinels must become
-- None before psycopg2 sees them, because float('nan') is adapted to a literal
-- Postgres NaN rather than to NULL.
--
-- FRED is the most likely place for it to happen again, and it would happen for
-- a new reason rather than the old one. Every one of these series carries
-- FRED's '.' missing sentinel — 380 of them across the loaded data — and the
-- tempting way to handle a string that will not parse is
-- pd.to_numeric(errors='coerce'), which yields NaN, not None. src/ingestion/fred.py
-- converts the sentinel explicitly in _to_value() for exactly this reason, and
-- this test is what proves that conversion still works after someone
-- "simplifies" it.
--
-- `= 'NaN'::numeric`, which reads wrong and is right. Under IEEE semantics NaN
-- is not equal to itself, so this predicate would match nothing for a float8
-- column — but Postgres deliberately departs from IEEE for NUMERIC, treating
-- NaN as equal to itself so that NUMERIC remains totally ordered and indexable.
-- The column here is NUMERIC(20,6), so equality is the correct and only
-- available test: Postgres has no `IS NAN` operator, and an earlier version of
-- this test used one and failed to compile.
--
-- If this column is ever widened to double precision, this predicate silently
-- stops matching and the test becomes vacuous.

select
    series_id,
    observation_date,
    value,
    'value is NaN — a pandas sentinel reached the database uncoerced' as reason
from {{ ref('stg_fred__observations') }}
where value is not null
  and value = 'NaN'::numeric
