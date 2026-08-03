{{
    config(
        materialized='view'
    )
}}

-- Staged FRED observations.
-- Grain: one row per (series_id, observation_date).
--
-- ------------------------------------------------------------------------
-- NO SANITY FILTER ON `value`, AND NO `where value is not null`.
--
-- Every other staging model in this project carries cheap invariants —
-- stg_polygon__prices drops rows with close <= 0. There is no equivalent here
-- and adding one would be wrong twice over.
--
-- First, macro series have no universal sign or range invariant. T10Y2Y is a
-- yield SPREAD and is legitimately negative — that is what an inverted yield
-- curve is, and it is the single most-watched recession signal in the dataset.
-- A `value > 0` filter copied from the price staging model would silently
-- delete exactly the observations anyone cares about. FEDFUNDS has been ~0.
-- CPIAUCSL is an index on an arbitrary base. There is no shared invariant to
-- assert.
--
-- Second, a NULL value here is DATA, not corruption: it is FRED's '.' sentinel,
-- meaning genuinely no observation for that period. Filtering it would make a
-- daily series look gap-free while quietly deleting every market holiday, and
-- would break the distinction between "no observation" and "no row", which is
-- the same distinction assert_no_missing_trading_days exists to police on the
-- price side.
--
-- The NULLs are carried through and counted instead, so a consumer can tell the
-- two apart and assert_macro_missing_values_are_expected can check that they
-- fall where they should.

with source as (

    select * from {{ source('raw', 'macro_observations') }}
    where source = 'fred'

),

renamed as (

    select
        series_id,

        -- The START OF THE PERIOD the value describes. NOT when it was known.
        -- January 2026's UNRATE is dated 2026-01-01 and was first published
        -- 2026-02-11. Anything joining on this column alone is asking "what
        -- period does this describe", never "what could I have known".
        observation_date,

        value,

        -- When the first estimate for this period became available. The column
        -- a point-in-time join must use. NULL for calculated series (T10Y2Y),
        -- which FRED publishes no initial-release history for.
        first_published_date,
        vintage_date,

        -- How long after the period began the number appeared. Materialised
        -- because it is the size of the look-ahead bias a naive join would
        -- introduce, and leaving it to be re-derived means it is never looked
        -- at. Measured here: ~35 days for UNRATE, ~121 for GDP, up to 175.
        (first_published_date - observation_date) as publication_lag_days,

        -- NULL means FRED reported '.'. Distinguished from "no row at all",
        -- which means the period was never fetched.
        (value is null) as is_missing_observation,

        source,
        ingested_at

    from source

)

select * from renamed
