{{
    config(
        materialized='table'
    )
}}

-- Current-state security dimension.
--
-- Grain: one row per security_id.
--
-- A view over the SCD2 snapshot filtered to `dbt_valid_to is null`, which is
-- SYSTEM time: "the version of this row the platform currently believes". It is
-- deliberately NOT `active = true`, which is a vendor attribute meaning "still
-- trading" — a delisted security still has exactly one current row here, and
-- dropping it would break every historical fact that joins to it.
--
-- The snapshot, not raw.security_master, even though the two agree on current
-- state today. Sourcing from the snapshot means the point-in-time variant of
-- this dimension is a one-line change to the filter (`:as_of between
-- dbt_valid_from and coalesce(dbt_valid_to, 'infinity')`) rather than a new
-- lineage, which is the whole reason ADR-0004 pays for the snapshot.
--
-- THE TWO TIME AXES. This model exposes both, under names that cannot be
-- confused, because conflating them is how a point-in-time claim turns out to be
-- false:
--
--   valid_from / valid_to   VALID time. When the SECURITY was tradeable.
--                           Vendor-supplied (list_date/delist_date). This is the
--                           axis a price bar is checked against — see
--                           fct_security_price_daily.
--   known_from / known_to   SYSTEM time. When the PLATFORM believed this row.
--                           dbt_valid_from/to from the snapshot. Answers "what
--                           did we think on date X", never "what was true".
--
-- known_to is always NULL in this model by construction, since it is filtered to
-- the current version. It is carried anyway so the column set is identical to
-- the point-in-time variant and a consumer cannot be surprised by its absence.

with snapshot as (

    select * from {{ ref('security_master_snapshot') }}
    where dbt_valid_to is null

),

renamed as (

    select
        security_id,

        -- An ATTRIBUTE, not the identity. Tickers are leased and reassigned;
        -- security_id is what anything else should join on (ADR-0007).
        ticker,
        name                as security_name,

        figi,
        share_class_figi,
        -- NULL by design, never derived. Carried rather than dropped so their
        -- emptiness is visible and a licensed deployment needs no model change.
        cusip,
        isin,

        exchange            as primary_exchange_mic,
        currency            as currency_code,
        security_type,
        active              as is_active,

        list_date           as valid_from,
        delist_date         as valid_to,

        dbt_valid_from      as known_from,
        dbt_valid_to        as known_to,

        source,
        ingested_at

    from snapshot

)

select * from renamed
