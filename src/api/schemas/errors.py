"""
The single error envelope every non-2xx response uses (ADR-0009 §4).

Uniform on purpose, including for Pydantic's own 422s: an API where the caller
needs two error parsers depending on which layer rejected them has a worse
contract than one that pays a small wrapping cost. FastAPI's native validation
body is nested under `details` intact rather than discarded, so the envelope is
consistent *and* nothing is lost.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """
    Stable, machine-readable failure slugs.

    These are part of the contract. `message` may be reworded freely; a value
    here may not be renamed without a version bump.
    """

    SECURITY_NOT_FOUND = "security_not_found"
    AMBIGUOUS_TICKER = "ambiguous_ticker"
    INVALID_RANGE = "invalid_range"
    RANGE_TOO_LARGE = "range_too_large"
    VALIDATION_ERROR = "validation_error"

    # Framework-level rejections: an unrouted path, a wrong method. Distinct
    # from SECURITY_NOT_FOUND on purpose — "this endpoint does not exist" and
    # "no security traded under that ticker" are different answers, and a
    # consumer branching on the code should not have to tell them apart from
    # the message.
    NOT_FOUND = "not_found"


class ApiError(BaseModel):
    """The body of every error this API raises."""

    error: ErrorCode = Field(
        description="Stable machine-readable code. Branch on this, not on `message`."
    )
    message: str = Field(
        description="Human-facing explanation. Wording is not part of the contract."
    )
    details: Any | None = Field(
        default=None,
        description=(
            "Optional structured payload. Carries Pydantic's per-field errors on "
            "a 422, and the competing security_ids on a 409."
        ),
    )
