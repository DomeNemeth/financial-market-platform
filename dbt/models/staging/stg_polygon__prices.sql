{{
    config(
        materialized='view'
    )
}}

with source as (

    select * from {{ source('raw', 'prices') }}
    where source = 'polygon'

),

renamed_and_filtered as (

    select
        ticker,
        trading_date,
        open        as open_price,
        high        as high_price,
        low         as low_price,
        close       as close_price,
        -- raw.volume is NUMERIC because Polygon reports fractional volume
        -- (fractional-share trades). Whole-share rounding belongs here, not in
        -- the raw layer — see raw.prices DDL in docker/postgres/init.sql.
        round(volume)::bigint as volume,
        vwap,
        trade_count,
        source,
        ingested_at

    from source

    where
        -- Basic sanity filters — should never fail on real data,
        -- but protects downstream models from corrupted rows
        close > 0
        and volume >= 0
        and high >= low

)

select * from renamed_and_filtered