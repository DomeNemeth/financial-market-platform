{{
    config(
        materialized='view'
    )
}}

-- Staged FRED series metadata.
-- Grain: one row per series_id.
--
-- Renaming and casting only, per ADR-0008. The one substantive choice is
-- normalising FRED's frequency vocabulary into a sortable rank, below.

with source as (

    select * from {{ source('raw', 'macro_series') }}
    where source = 'fred'

),

renamed as (

    select
        series_id,
        title,

        frequency,
        frequency_short,

        -- A sortable ordering over the frequency vocabulary, so "which of these
        -- series is coarser" is answerable in SQL. FRED's own strings are not
        -- orderable — 'M' < 'Q' < 'W' alphabetically puts weekly last, which is
        -- exactly backwards.
        --
        -- This matters because the grain mismatch is the whole problem the
        -- macro layer exists to handle: a quarterly series joined to a daily
        -- price series repeats each value ~63 times, and knowing which side is
        -- coarser is what tells a consumer whether that is expected.
        case frequency_short
            when 'D'  then 1
            when 'W'  then 2
            when 'BW' then 3
            when 'M'  then 4
            when 'Q'  then 5
            when 'SA' then 6
            when 'A'  then 7
        end as frequency_rank,

        units,
        units_short,

        seasonal_adjustment,
        seasonal_adjustment_short,
        -- SAAR ("seasonally adjusted annual rate") is seasonally adjusted AND
        -- annualised, so it is not comparable with a plain SA series without
        -- knowing that. Flagged rather than left for a reader to spot in a
        -- string.
        (seasonal_adjustment_short = 'SAAR') as is_annualised_rate,

        observation_start,
        observation_end,
        last_updated,
        notes,

        source,
        ingested_at

    from source

)

select * from renamed
