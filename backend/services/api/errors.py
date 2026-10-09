"""A stable error taxonomy, and a correlation ID that reaches the audit ledger.

Why a taxonomy rather than HTTP status codes
--------------------------------------------
Another department's system is going to integrate with this API, and it has to branch on
what went wrong. HTTP status alone does not support that: a 422 from this platform can
mean a malformed window, an unknown camera, a window that exceeds the aggregation
maximum, or a corridor with one camera in it. A client that wants to retry only the
transient ones has to match on the English prose of `detail`, which is not a contract --
it is a string that changes the first time someone improves a message.

So every error carries a `code` from a closed set, documented, and never reworded. The
message is free to improve; the code is not free to change.

Why the correlation ID is on every response and not only on errors
-------------------------------------------------------------------
A support conversation starts with "it failed at about four o'clock". With an id on every
response, a client can quote the exact request, and that id appears in the structured log
line *and*, for an audited action, in the ledger entry. One identifier links what the
client saw, what the server logged, and what the system recorded itself doing.

Putting it only on errors would be the obvious economy and the wrong one: the request a
client most wants to ask about is often the one that *succeeded* and returned something
surprising.

`retryable` is advice the client can act on
--------------------------------------------
Each code says whether retrying the identical request could plausibly succeed. A
validation failure never will; a rate-limited or dependency-unavailable request will.
Returning this is the difference between a well-behaved integration and one that either
hammers a broken endpoint or gives up on a transient blip.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from services.common import structured_log

log = logging.getLogger(__name__)

#: Header carrying the correlation id on every response, and accepted on a request so a
#: caller's own trace id flows through rather than being replaced.
CORRELATION_HEADER = "X-Correlation-ID"

#: Where the id lives for the duration of a request, so audit entries and log lines can
#: reach it without threading it through every signature.
_CONTEXT_KEY = "setu_correlation_id"


class ErrorCode(StrEnum):
    """Stable, machine-readable error codes. **Never reworded, never reused.**

    Adding a member is a compatible change. Changing or removing one is not, and the
    deprecation policy in `docs/API_VERSIONING.md` applies.
    """

    # --- the caller's request ---
    VALIDATION_FAILED = "validation_failed"
    WINDOW_INVALID = "window_invalid"
    WINDOW_TOO_LARGE = "window_too_large"
    RESOURCE_NOT_FOUND = "resource_not_found"
    CONFLICT = "conflict"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    CURSOR_INVALID = "cursor_invalid"

    # --- the caller's identity ---
    AUTHENTICATION_REQUIRED = "authentication_required"
    CREDENTIALS_INVALID = "credentials_invalid"
    TOKEN_EXPIRED = "token_expired"
    PERMISSION_DENIED = "permission_denied"
    API_KEY_REVOKED = "api_key_revoked"
    RATE_LIMITED = "rate_limited"

    # --- this platform ---
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    FEED_UNAVAILABLE = "feed_unavailable"
    FEED_QUOTA_EXHAUSTED = "feed_quota_exhausted"
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"


#: Which codes a client may retry unchanged. Everything absent is permanent: retrying it
#: produces the same answer and wastes both sides' capacity.
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.FEED_UNAVAILABLE,
        ErrorCode.FEED_QUOTA_EXHAUSTED,
        ErrorCode.INTERNAL_ERROR,
    }
)

#: The HTTP status each code is returned with. Stated once, here, so the same condition
#: cannot come back as a 400 from one router and a 422 from another.
#:
#: Written as integers rather than Starlette's constants. Two of the ones needed here
#: are deprecated in the pinned Starlette and emit a warning at import, and the
#: numbers are both stable and more readable in a table this dense.
STATUS_FOR: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.WINDOW_INVALID: 422,
    ErrorCode.WINDOW_TOO_LARGE: 422,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.CURSOR_INVALID: 422,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.CREDENTIALS_INVALID: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.API_KEY_REVOKED: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.FEED_UNAVAILABLE: 503,
    ErrorCode.FEED_QUOTA_EXHAUSTED: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.NOT_IMPLEMENTED: 501,
}


@dataclass
class ApiError(Exception):
    """Raise this instead of HTTPException anywhere a client might branch on the cause."""

    code: ErrorCode
    message: str
    #: Extra structure for the client. Must never contain a credential, a plate the
    #: caller may not see, or anything else the response body would not otherwise carry.
    detail: dict[str, Any] | None = None
    #: Seconds to wait before retrying. Only meaningful for a retryable code; returned
    #: as the standard `Retry-After` header as well as in the body.
    retry_after_s: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def status_code(self) -> int:
        return STATUS_FOR[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def body(self, correlation_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        return {"error": payload}


def new_correlation_id() -> str:
    """A fresh id. Short enough to read aloud on a support call."""
    return uuid.uuid4().hex[:16]


def correlation_id_of(request: Request) -> str:
    """The id for this request, generating one if the middleware has not run.

    The fallback exists so a unit test exercising a handler directly does not have to
    install the middleware, and so a code path reached outside a request cycle gets an
    id rather than a KeyError.
    """
    existing = getattr(request.state, _CONTEXT_KEY, None)
    if isinstance(existing, str):
        return existing
    generated = new_correlation_id()
    setattr(request.state, _CONTEXT_KEY, generated)
    return generated


def bind_correlation_id(request: Request, value: str) -> None:
    setattr(request.state, _CONTEXT_KEY, value)


def _sanitise_inbound(value: str | None) -> str | None:
    """Accept a caller's own trace id, within limits.

    Echoing an arbitrary header into logs and into the audit ledger is an injection
    route: a newline splits a log line in two, and 8 KB of it fills a disk. Hex-ish and
    short, or we generate our own.
    """
    if not value:
        return None
    candidate = value.strip()
    if 8 <= len(candidate) <= 64 and all(c.isalnum() or c in "-_" for c in candidate):
        return candidate
    return None


async def correlation_middleware(request: Request, call_next: Any) -> Any:
    """Attach a correlation id to every request and echo it on every response."""
    correlation = _sanitise_inbound(request.headers.get(CORRELATION_HEADER)) or new_correlation_id()
    bind_correlation_id(request, correlation)
    # Also on the logging context, so every line emitted while handling this request
    # carries the id without any of them having to be passed it. Threading it through
    # every function that might log is the change that gets half-done, and the half
    # that was missed is where the interesting failure happens.
    structured_log.bind(correlation)
    response = await call_next(request)
    response.headers[CORRELATION_HEADER] = correlation
    return response


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an ApiError, with its correlation id in the body and the header."""
    assert isinstance(exc, ApiError)
    correlation = correlation_id_of(request)
    # Logged at the server with the same id the client is about to see, which is the
    # whole point of the identifier.
    log.info(
        "api_error code=%s status=%d correlation=%s path=%s",
        exc.code.value,
        exc.status_code,
        correlation,
        request.url.path,
    )
    headers = {CORRELATION_HEADER: correlation}
    if exc.retry_after_s is not None:
        headers["Retry-After"] = str(exc.retry_after_s)
    return JSONResponse(status_code=exc.status_code, content=exc.body(correlation), headers=headers)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Anything not already an ApiError.

    The exception text is logged and **not** returned. An unhandled error's message is
    whatever Python happened to produce -- a SQL fragment, a file path, a connection
    string -- and none of that belongs in a response to another department's system.
    The correlation id is what lets them report it anyway.
    """
    correlation = correlation_id_of(request)
    log.exception("unhandled error correlation=%s path=%s", correlation, request.url.path)
    error = ApiError(
        ErrorCode.INTERNAL_ERROR,
        "an unexpected error occurred; quote the correlation id when reporting it",
    )
    return JSONResponse(
        status_code=error.status_code,
        content=error.body(correlation),
        headers={CORRELATION_HEADER: correlation},
    )


__all__ = [
    "CORRELATION_HEADER",
    "RETRYABLE",
    "STATUS_FOR",
    "ApiError",
    "ErrorCode",
    "api_error_handler",
    "bind_correlation_id",
    "correlation_id_of",
    "correlation_middleware",
    "new_correlation_id",
    "unhandled_error_handler",
]
