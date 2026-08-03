{{
    config(
        materialized='table'
    )
}}

-- The macro observation fact.
-- Grain: one row per (series_id, observation_date).
--
-- Deliberately keeps the vendor's own grain rather than resampling everything
-- onto a daily calendar. A quarterly GDP print is one observation, not sixty-three
-- identical ones, and materialising the repetition here would triple the row
-- count while destroying the ability to ask "how many times has GDP actually
-- been reported". The grain mismatch is resolved where it is needed — in
-- fct_security_price_macro_context — and only there.
--
-- Both time axes are carried, under names that cannot be confused:
--   observation_date      valid time  — the period the number describes
--   first_published_date  system time — when anyone could first have known it
--
-- That is the same discipline dim_security applies to list_date/delist_date
-- against dbt_valid_from/to, and for the same reason: the two answer different
-- questions and a model that exposes only one of them invites the wrong join.

with observations as (

    select * from {{ ref('stg_fred__observations') }}

),

series as (

    select * from {{ ref('dim_macro_series') }}

)

select
    o.series_id,
    o.observation_date,

    o.value,
    o.is_missing_observation,

    o.first_published_date,
    o.vintage_date,
    o.publication_lag_days,

    -- Denormalised from the dimension so a consumer reading this fact cannot
    -- plot a percent against an index by accident. Units are not decoration
    -- here: GDP is billions of dollars, UNRATE is a percent, CPIAUCSL is an
    -- index on an arbitrary 1982-84 base, and nothing in `value` says which.
    s.title,
    s.frequency_short,
    s.units_short,
    s.seasonal_adjustment_short,
    s.is_annualised_rate,
    s.supports_point_in_time_join,

    o.ingested_at,
    {{ dbt.current_timestamp() }} as built_at

from observations o
left join series s
    on s.series_id = o.series_id
