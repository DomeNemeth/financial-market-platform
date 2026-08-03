"""Response schema for the security dimension."""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class SecurityOut(BaseModel):
    """
    One security, as of a resolution date.

    BOTH TIME AXES ARE EXPOSED, under the same unambiguous names dim_security
    uses. That is not redundancy — conflating them is how a point-in-time claim
    turns out to be false (ADR-0004), and an API that surfaced only one would
    force the confusion back on the consumer:

      valid_from / valid_to   VALID time. When the SECURITY was tradeable.
                              Vendor-supplied. This is the axis `as_of` filters.
      known_from / known_to   SYSTEM time. When this PLATFORM believed the row.
                              Answers "what did we think on date X", never
                              "what was true". `as_of` does NOT filter on it —
                              see ADR-0009 §3.
    """

    model_config = ConfigDict(from_attributes=True)

    security_id: int = Field(
        description="Durable surrogate anchored on FIGI. The only correct join key (ADR-0007)."
    )
    ticker: str = Field(
        description=(
            "Current ticker. An ATTRIBUTE, not the identity — tickers are leased "
            "by exchanges and reassigned to unrelated companies."
        )
    )
    security_name: str | None = None

    figi: str | None = Field(
        default=None, description="OpenFIGI composite FIGI. Free and redistributable."
    )
    share_class_figi: str | None = None
    # Present and null, never derived. Carried rather than dropped so their
    # emptiness is visible: a checksum-valid fabricated identifier is worse than
    # a missing one (ADR-0007).
    cusip: str | None = Field(
        default=None, description="Licensed. NULL in this deployment by design."
    )
    isin: str | None = Field(default=None, description="Licensed. NULL by design, as with cusip.")

    primary_exchange_mic: str | None = None
    currency_code: str | None = None
    security_type: str | None = None
    is_active: bool | None = Field(
        default=None,
        description=(
            "The vendor's 'still trading' flag. NOT the same question as whether "
            "`as_of` fell inside the valid-time window."
        ),
    )

    valid_from: dt.date | None = Field(
        default=None,
        description="VALID-time start: when the security became tradeable. NULL means unknown.",
    )
    valid_to: dt.date | None = Field(
        default=None, description="VALID-time end. NULL means still listed."
    )
    known_from: dt.datetime | None = Field(
        default=None,
        description="SYSTEM-time start: when this platform first believed this version.",
    )
    known_to: dt.datetime | None = Field(
        default=None,
        description=(
            "SYSTEM-time end. Always null here, which serves current-state rows; "
            "carried so the column set would not change under a point-in-time variant."
        ),
    )

    source: str | None = None
    ingested_at: dt.datetime | None = None

    resolved_as_of: dt.date = Field(
        description=(
            "The valid-time date this lookup resolved against. Echoed because it "
            "defaults, and a caller must be able to see what the default was."
        )
    )
