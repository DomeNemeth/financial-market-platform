"""
The exceptions the API raises, and the handlers that render them.

Every non-2xx response leaves this application with the same body shape
(ADR-0009 §4). That includes FastAPI's own validation errors and its routing
404s, which are re-wrapped rather than left in their native shapes — an API where
the caller needs a different parser depending on which layer rejected them has a
worse contract than one that pays a small wrapping cost. Nothing is discarded in
the wrapping: Pydantic's per-field errors are nested under `details` intact.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.api.schemas.errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)


class ApiException(Exception):
    """
    Base for every failure this application raises deliberately.

    Carries the status code and the stable slug together so a raise site states
    the whole contract in one place, rather than leaving the status to be chosen
    by whatever handler happens to catch it.
    """

    status_code: int = 500
    code: ErrorCode = ErrorCode.NOT_FOUND

    def __init__(self, message: str, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class SecurityNotFound(ApiException):
    """No security was listed under the requested ticker as of the resolution date."""

    status_code = 404
    code = ErrorCode.SECURITY_NOT_FOUND


class AmbiguousTicker(ApiException):
    """
    The ticker resolved to more than one security as of the resolution date.

    A 409 rather than picking one. This is a data defect — two securities whose
    valid-time windows overlap while sharing a ticker — and the entire reason
    ADR-0007 exists is that silently choosing between them splices unrelated
    companies together. It is the API-level mirror of the dbt test that fails
    when a price bar resolves to several securities.
    """

    status_code = 409
    code = ErrorCode.AMBIGUOUS_TICKER


class InvalidRange(ApiException):
    """`start` is after `end`."""

    status_code = 400
    code = ErrorCode.INVALID_RANGE


class RangeTooLarge(ApiException):
    """
    The requested window would return more rows than the cap allows.

    Rejected rather than truncated. A truncated series looks exactly like a
    complete one, and this project's recurring failure mode to avoid is data
    that is silently incomplete rather than loudly absent.
    """

    status_code = 400
    code = ErrorCode.RANGE_TOO_LARGE


def _render(
    status_code: int, code: ErrorCode, message: str, details: object | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        # mode="json" so nested Decimals/dates in `details` serialise the same
        # way they would in a success body.
        content=ApiError(error=code, message=message, details=details).model_dump(mode="json"),
    )


async def api_exception_handler(request: Request, exc: ApiException) -> JSONResponse:
    logger.info(f"{exc.code.value} on {request.url.path}: {exc.message}")
    return _render(exc.status_code, exc.code, exc.message, exc.details)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    422s, in the house envelope with Pydantic's field-level detail preserved.

    `exc.errors()` can contain non-JSON-native values (a ValueError instance
    under "ctx", bytes under "input"), so it is round-tripped through Pydantic's
    JSON serialiser rather than handed to JSONResponse directly — otherwise a
    malformed request could turn a 422 into a 500 while trying to describe it.
    """
    return _render(
        422,
        ErrorCode.VALIDATION_ERROR,
        "Request validation failed.",
        details=ApiError(
            error=ErrorCode.VALIDATION_ERROR, message="", details=exc.errors()
        ).model_dump(mode="json")["details"],
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Framework-raised HTTP errors — an unrouted path, a disallowed method.

    Wrapped so that a caller who typos a URL gets the same body shape as one who
    typos a ticker. The slug is NOT_FOUND rather than SECURITY_NOT_FOUND: those
    are different answers and the code should say which.
    """
    return _render(exc.status_code, ErrorCode.NOT_FOUND, str(exc.detail))


def register_error_handlers(app: FastAPI) -> None:
    """Install every handler. Called once from main.py."""
    app.add_exception_handler(ApiException, api_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
