{{
    config(
        materialized='view'
    )
}}

-- Staged security master from Polygon.
-- Grain: one row per security_id (current state only — history lives in the
-- security_master_snapshot, per ADR-0004).

with source as (

    select * from {{ source('raw', 'security_master') }}
    where source = 'polygon'

),

renamed as (

    select
        security_id,
        ticker,
        name            as security_name,

        figi,
        share_class_figi,
        -- Carried through, always NULL by design. Kept in the model rather than
        -- dropped so a licensed deployment populating raw needs no model change,
        -- and so their emptiness is visible rather than hidden. See ADR-0007.
        cusip,
        isin,

        exchange        as primary_exchange_mic,
        currency        as currency_code,
        security_type,
        active          as is_active,

        -- VALID time — when the security was tradeable. Distinct from the
        -- snapshot's dbt_valid_from/to, which are SYSTEM time.
        list_date       as valid_from,
        delist_date     as valid_to,

        source,
        ingested_at

    from source

)

select * from renamed
