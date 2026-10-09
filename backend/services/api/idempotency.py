"""Idempotency keys: a retried request must not write twice.

The case this exists for
------------------------
A department's integration POSTs a camera import. The connection drops after the server
committed but before the response arrived. The client, correctly, retries.

Without idempotency the estate now has the import twice. With a naive
"reject duplicate keys" implementation the client gets a 409 and still does not know
whether the first attempt worked -- which is the same uncertainty it started with, now
with an error attached.

So a replay returns **the original response, verbatim**. The client that retried because
it never saw the first answer now gets that answer. That is the whole point, and it is
the part a naive implementation gets backwards.

The scope is (key, endpoint, actor)
------------------------------------
The same key from a different caller is a different request -- keys are client-chosen
and two integrations will eventually pick the same UUID. The same key against a
different endpoint is a client bug, and surfacing it beats silently serving the wrong
cached body.

The body hash is part of the contract
--------------------------------------
A client reusing one key with a *different* body is a bug on their side. Returning the
first response would hide both the bug and the fact that their second request did
nothing. So that case is a conflict with an explicit message, which is the one situation
where refusing is more useful than replaying.

In-flight requests
------------------
A record is written *before* the work runs, with no response attached. A second request
arriving while the first is still in flight gets a conflict telling it to wait rather
than a duplicate execution -- the retry that arrives three seconds later, because a
client's timeout is shorter than the server's work, is the common case rather than an
exotic one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api.errors import ApiError, ErrorCode

log = logging.getLogger(__name__)

#: Header a client sends to make a request idempotent.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: How long a key is remembered. 24 hours covers any retry a sane client performs and
#: keeps the table from becoming an append-only log of every mutation ever made.
RETENTION = timedelta(hours=24)

#: Longest key accepted. Clients use UUIDs; anything much longer is a mistake or an
#: attempt to use the table as storage.
MAX_KEY_LENGTH = 255


@dataclass(frozen=True)
class Replay:
    """A previously completed response, to be returned again unchanged."""

    status_code: int
    body: dict[str, Any]


def hash_request(body: Any) -> str:
    """A stable digest of a request body.

    Sorted keys and fixed separators, so the same logical request hashes the same way
    regardless of how the client's JSON encoder ordered it.
    """
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def begin(
    session: Session,
    *,
    key: str,
    endpoint: str,
    actor: str,
    request_body: Any,
) -> Replay | None:
    """Claim this key, or return the response a previous identical request produced.

    Returns `None` when the caller should go ahead and do the work, and a `Replay` when
    it should return that instead. Raises `ApiError` for the two cases that are neither:
    a key reused with a different body, and a key whose first request is still running.
    """
    if not key or len(key) > MAX_KEY_LENGTH:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{IDEMPOTENCY_HEADER} must be 1-{MAX_KEY_LENGTH} characters",
        )

    digest = hash_request(request_body)
    now = datetime.now(timezone.utc)

    existing = (
        session.execute(
            text(
                "SELECT id, request_hash, response_status, response_body, completed_at,"
                "       expires_at"
                " FROM idempotency_key"
                " WHERE key = :key AND endpoint = :endpoint AND actor = :actor"
            ),
            {"key": key, "endpoint": endpoint, "actor": actor},
        )
        .mappings()
        .first()
    )

    if existing is not None and existing["expires_at"] > now:
        if existing["request_hash"] != digest:
            # The one case where refusing beats replaying: the client sent a different
            # body under the same key, so neither answer is right for them.
            raise ApiError(
                ErrorCode.IDEMPOTENCY_KEY_REUSED,
                f"this {IDEMPOTENCY_HEADER} was already used for a different request body",
                detail={"endpoint": endpoint},
            )
        if existing["completed_at"] is None:
            # A client whose timeout is shorter than the server's work. Common, not
            # exotic. Telling it to wait beats running the work a second time.
            raise ApiError(
                ErrorCode.CONFLICT,
                "a request with this key is still in progress; retry shortly",
                retry_after_s=5,
            )
        return Replay(
            status_code=int(existing["response_status"] or 200),
            body=existing["response_body"] or {},
        )

    if existing is not None:
        # Expired. Replace it rather than leaving a stale row to collide with.
        session.execute(text("DELETE FROM idempotency_key WHERE id = :id"), {"id": existing["id"]})

    session.execute(
        text(
            "INSERT INTO idempotency_key (id, key, endpoint, actor, request_hash,"
            " created_at, expires_at)"
            " VALUES (:id, :key, :endpoint, :actor, :hash, now(), :expires)"
        ),
        {
            "id": uuid.uuid4(),
            "key": key,
            "endpoint": endpoint,
            "actor": actor,
            "hash": digest,
            "expires": now + RETENTION,
        },
    )
    session.flush()
    return None


def complete(
    session: Session,
    *,
    key: str,
    endpoint: str,
    actor: str,
    status_code: int,
    response_body: Any,
) -> None:
    """Record the response, so a later retry replays it.

    Called after the work succeeded. A failed request deliberately leaves no completed
    record: a client retrying after a 500 should get a real attempt, not a cached
    failure.
    """
    session.execute(
        text(
            "UPDATE idempotency_key"
            "   SET response_status = :status,"
            "       response_body = CAST(:body AS jsonb),"
            "       completed_at = now()"
            " WHERE key = :key AND endpoint = :endpoint AND actor = :actor"
        ),
        {
            "key": key,
            "endpoint": endpoint,
            "actor": actor,
            "status": status_code,
            "body": json.dumps(response_body, default=str),
        },
    )
    session.flush()


def abandon(session: Session, *, key: str, endpoint: str, actor: str) -> None:
    """Release a claim whose work failed, so the client can retry properly.

    Without this, a request that raised would hold its key until expiry and every retry
    would be told "still in progress" for twenty-four hours.
    """
    session.execute(
        text(
            "DELETE FROM idempotency_key"
            " WHERE key = :key AND endpoint = :endpoint AND actor = :actor"
            "   AND completed_at IS NULL"
        ),
        {"key": key, "endpoint": endpoint, "actor": actor},
    )
    session.flush()


def purge_expired(session: Session) -> int:
    """Drop expired records. Safe to run from anywhere, including a cron."""
    from typing import cast

    from sqlalchemy.engine import CursorResult

    result = cast(
        CursorResult[Any],
        session.execute(text("DELETE FROM idempotency_key WHERE expires_at <= now()")),
    )
    session.flush()
    return int(result.rowcount or 0)


__all__ = [
    "IDEMPOTENCY_HEADER",
    "MAX_KEY_LENGTH",
    "RETENTION",
    "Replay",
    "abandon",
    "begin",
    "complete",
    "hash_request",
    "purge_expired",
]
