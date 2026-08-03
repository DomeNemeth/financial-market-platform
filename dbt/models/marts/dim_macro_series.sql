{{
    config(
        materialized='table'
    )
}}

-- The macro series dimension.
-- Grain: one row per series_id.
--
-- The natural key IS the primary key. There is no surrogate, and unlike
-- dim_security that is not a simplification — see migrations/0005_macro_data.sql
-- for why the three properties that force a surrogate for securities (ticker
-- reuse, multiple issuing authorities, vendor-specific spellings) are all absent
-- for a FRED series ID.
--
-- Carries the eligibility flag the point-in-time join depends on, so that
-- "why is DGS10 missing from the macro context" has an answer in the dimension
-- rather than in a WHERE clause three models away.

with series as (

    select * from {{ ref('stg_fred__series') }}

),

observations as (

    select
        series_id,
        count(*)                                        as observation_count,
        count(value)                                    as observed_value_count,
        count(*) filter (where is_missing_observation)   as missing_value_count,
        count(first_published_date)                      as published_date_count,
        min(observation_date)                            as first_observation_date,
        max(observation_date)                            as last_observation_date,
        min(publication_lag_days)                        as min_publication_lag_days,
        round(avg(publication_lag_days))                 as avg_publication_lag_days,
        max(publication_lag_days)                        as max_publication_lag_days

    from {{ ref('stg_fred__observations') }}
    group by 1

)

select
    s.series_id,
    s.title,

    s.frequency,
    s.frequency_short,
    s.frequency_rank,

    s.units,
    s.units_short,
    s.seasonal_adjustment,
    s.seasonal_adjustment_short,
    s.is_annualised_rate,

    -- Valid time: the span the series describes.
    o.first_observation_date,
    o.last_observation_date,
    s.observation_start as vendor_observation_start,
    s.observation_end   as vendor_observation_end,

    -- System time: when FRED last revised anything in this series. The macro
    -- analogue of the price mart's actions_observed_through — a macro series is
    -- not a fixed historical object either, and a value quoted without this is
    -- a value quoted without saying which revision it is.
    s.last_updated,

    o.observation_count,
    o.observed_value_count,
    -- Counts of FRED's '.' sentinel. Expected and benign for daily series,
    -- which carry one per market holiday; a large count on a MONTHLY series
    -- would be a real gap worth investigating.
    o.missing_value_count,

    o.min_publication_lag_days,
    o.avg_publication_lag_days,
    o.max_publication_lag_days,

    -- ------------------------------------------------------------------
    -- Whether this series can take part in a point-in-time join at all.
    --
    -- FALSE for calculated series — T10Y2Y is a spread FRED derives from two
    -- other series, so it has no initial-release history and no publication
    -- date. The daily Treasury constant-maturity series (DGS10, DGS2) come back
    -- the same way.
    --
    -- Such series are EXCLUDED from fct_security_price_macro_context rather
    -- than joined with an assumed lag. Assuming one would mean inventing a
    -- publication date, which is the same category of error as fabricating a
    -- CUSIP (ADR-0007) or a vwap (ADR-0006): plausible, uncheckable, and wrong
    -- in a way that flatters a backtest. Their absence is visible here.
    -- ------------------------------------------------------------------
    (o.published_date_count > 0) as supports_point_in_time_join,
    o.published_date_count,

    {{ dbt.current_timestamp() }} as built_at

from series s
left join observations o
    on o.series_id = s.series_id
