"""Webhooks: how another department's control room learns about an alert.

Without this, a department integrates by polling, and polling has two failure modes that
both look like the platform's fault. Poll too slowly and alerts arrive late; poll fast
enough to be useful and the API spends its capacity answering "anything yet?" several
thousand times for every real event.

Durable rows, not an in-memory queue
-------------------------------------
Every useful property here -- at-least-once delivery, exponential backoff, a dead-letter
view, manual replay -- is a property of rows that survive a restart. An in-process queue
loses its contents exactly when a receiver is failing, which is the moment the retries
mattered. So a delivery is a row, the dispatcher is a loop over due rows, and a redeploy
mid-retry costs nothing.

At-least-once, and saying so
----------------------------
A receiver **will** see a duplicate eventually: a response that times out after the
receiver committed is indistinguishable from one that never arrived, and the only safe
move is to retry. Exactly-once is not available over HTTP without a two-phase protocol
nobody is going to implement for a control-room integration.

So every payload carries an `event_id`, and the documentation tells receivers to
deduplicate on it. Handing them the means to cope is the honest alternative to claiming
a guarantee that cannot be kept.

HMAC, and the two things people get wrong about it
---------------------------------------------------
The signature is `HMAC-SHA256(secret, timestamp + "." + body)` over the **exact bytes**
sent, not over a re-serialised dict -- a receiver that re-encodes JSON before verifying
gets a different byte string and a failing signature, which is the single most common
webhook integration bug.

The timestamp is inside the signed material rather than beside it, which is what makes
the signature replay-resistant: without it, an attacker who captures one valid request
can resend it forever. Receivers are told to reject a timestamp outside a few minutes.

Backoff, and when to stop
--------------------------
Exponential with jitter. Jitter because a receiver that comes back after an outage would
otherwise be hit by every queued delivery in the same instant -- the retry storm that
knocks it straight back over.

After `MAX_ATTEMPTS` a delivery is `dead`: kept, visible, and replayable by hand, not
discarded. A subscription that fails `FAILURE_DISABLE_THRESHOLD` times consecutively is
disabled, because retrying a permanently broken endpoint forever is how a queue becomes
a bill.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Header carrying the signature, and the one carrying the timestamp it covers.
SIGNATURE_HEADER = "X-SETU-Signature"
TIMESTAMP_HEADER = "X-SETU-Timestamp"
EVENT_ID_HEADER = "X-SETU-Event-Id"
EVENT_TYPE_HEADER = "X-SETU-Event-Type"

#: Signature scheme marker, so a future change to the algorithm is unambiguous on the
#: wire rather than being inferred from a length.
SIGNATURE_VERSION = "v1"

#: How long a receiver is given to answer. Short: a control room's endpoint that takes
#: longer than this is one that should be queueing internally, and holding the
#: dispatcher open is how one slow subscriber delays everyone else's alerts.
DELIVERY_TIMEOUT_S = 10.0

#: Attempts before a delivery is parked in the dead-letter view.
MAX_ATTEMPTS = 6

#: First retry delay. Doubles each attempt: 30s, 1m, 2m, 4m, 8m -- about 15 minutes of
#: trying, which covers a deploy or a restart without pursuing a dead endpoint for days.
BASE_BACKOFF_S = 30.0

#: Ceiling on a single wait, so the last attempts do not drift hours apart.
MAX_BACKOFF_S = 900.0

#: Consecutive failures before a subscription is switched off. Deliveries already
#: queued stay queued and replayable; nothing new is attempted until someone re-enables
#: it, which forces a human to notice.
FAILURE_DISABLE_THRESHOLD = 20


class WebhookError(RuntimeError):
    """Any failure here. Messages never contain a secret."""


@dataclass(frozen=True)
class SignedPayload:
    body: bytes
    timestamp: str
    signature: str

    def headers(self, event_id: str, event_type: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: self.signature,
            TIMESTAMP_HEADER: self.timestamp,
            EVENT_ID_HEADER: event_id,
            EVENT_TYPE_HEADER: event_type,
            "User-Agent": "setu-webhooks/1.0",
        }


def sign(payload: dict[str, Any], secret: str, *, at: datetime | None = None) -> SignedPayload:
    """Sign a payload, returning the exact bytes that must be sent.

    The body is returned alongside the signature on purpose. A caller that re-serialises
    the dict before sending produces different bytes and a signature the receiver cannot
    verify -- the single most common webhook integration bug, and one that is impossible
    to make here because the signed bytes are the ones handed back.
    """
    moment = at or datetime.now(timezone.utc)
    timestamp = str(int(moment.timestamp()))
    # Deterministic separators and sorted keys so the same payload always produces the
    # same bytes, which makes a signature reproducible during a dispute.
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return SignedPayload(body=body, timestamp=timestamp, signature=f"{SIGNATURE_VERSION}={digest}")


def verify(
    body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    *,
    tolerance_s: int = 300,
    now: datetime | None = None,
) -> bool:
    """Verify a signature the way a receiver should. Shipped so they can copy it.

    Published here rather than only described in documentation, because every receiver
    has to implement this and most of them will get the timestamp binding wrong.
    """
    if not signature.startswith(f"{SIGNATURE_VERSION}="):
        return False
    try:
        age = abs((now or datetime.now(timezone.utc)).timestamp() - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > tolerance_s:
        # Without this, a captured request is replayable forever.
        return False

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature.split("=", 1)[1], expected)


def backoff_delay(attempt: int) -> float:
    """Seconds to wait before attempt `attempt`, with jitter.

    Jitter matters more than the curve. A receiver recovering from an outage would
    otherwise be hit by every queued delivery in the same instant -- the retry storm
    that knocks it straight back over.
    """
    nominal = min(BASE_BACKOFF_S * (2 ** max(0, attempt - 1)), MAX_BACKOFF_S)
    return float(nominal * random.uniform(0.75, 1.25))  # noqa: S311 -- jitter, not a secret


def enqueue(
    session: Session,
    *,
    event_type: str,
    payload: dict[str, Any],
    department_id: uuid.UUID | None,
    event_id: str | None = None,
) -> int:
    """Queue this event for every subscription that wants it. Returns how many.

    Matching is by event type and department. An estate-wide subscription (NULL
    department) receives everything; a departmental one receives only its own, which is
    the same boundary RLS enforces on every other read.
    """
    identifier = event_id or uuid.uuid4().hex
    rows = (
        session.execute(
            text(
                "SELECT id FROM webhook_subscription"
                " WHERE active AND disabled_at IS NULL"
                "   AND event_types @> CAST(:event AS jsonb)"
                "   AND (department_id IS NULL OR department_id = :dept)"
            ),
            {"event": json.dumps([event_type]), "dept": department_id},
        )
        .scalars()
        .all()
    )

    for subscription_id in rows:
        session.execute(
            text(
                "INSERT INTO webhook_delivery (id, subscription_id, event_type, event_id,"
                " payload, created_at, attempts, next_attempt_at, state, attempt_log)"
                " VALUES (gen_random_uuid(), :sub, :type, :eid, CAST(:payload AS jsonb),"
                " now(), 0, now(), 'pending', '[]'::jsonb)"
            ),
            {
                "sub": subscription_id,
                "type": event_type,
                "eid": identifier,
                "payload": json.dumps(payload),
            },
        )
    session.flush()
    return len(rows)


@dataclass
class DispatchResult:
    attempted: int = 0
    delivered: int = 0
    retrying: int = 0
    dead: int = 0
    subscriptions_disabled: int = 0


def _record_attempt(
    session: Session,
    delivery_id: uuid.UUID,
    *,
    attempt: int,
    status_code: int | None,
    error: str | None,
    state: str,
    next_attempt_at: datetime | None,
) -> None:
    """Append this attempt to the log and move the delivery's state.

    Every attempt, not just the final outcome. "Delivered" and "delivered on the fourth
    attempt after two timeouts" are different operational facts, and only the second one
    tells a department their endpoint is sick.
    """
    entry = {
        "attempt": attempt,
        "at": datetime.now(timezone.utc).isoformat(),
        "status_code": status_code,
        "error": error,
    }
    session.execute(
        text(
            # `:state` is cast at every use. Postgres deduces a parameter's type from
            # its first use, and this one appears both as a varchar assignment and
            # inside a text comparison below -- which it rejects with "inconsistent
            # types deduced for parameter $2" rather than coercing.
            "UPDATE webhook_delivery"
            "   SET attempts = :attempt,"
            "       state = CAST(:state AS varchar(16)),"
            "       last_status_code = :status,"
            "       last_error = :error,"
            "       next_attempt_at = :next,"
            "       delivered_at = CASE WHEN CAST(:state AS varchar(16)) = 'delivered'"
            "                          THEN now() ELSE delivered_at END,"
            "       attempt_log = attempt_log || CAST(:entry AS jsonb)"
            " WHERE id = :id"
        ),
        {
            "id": delivery_id,
            "attempt": attempt,
            "state": state,
            "status": status_code,
            "error": error,
            "next": next_attempt_at,
            "entry": json.dumps([entry]),
        },
    )


def dispatch_due(
    session: Session,
    *,
    limit: int = 50,
    now: datetime | None = None,
    sender: Any = None,
) -> DispatchResult:
    """Attempt every delivery that is due. Never raises.

    `sender` is injectable so the retry and dead-letter behaviour can be tested without
    a network, in the same style as the ingest contract tests.
    """
    from services.common.credentials import CredentialError, SealedCredential, unseal

    moment = now or datetime.now(timezone.utc)
    result = DispatchResult()

    due = (
        session.execute(
            text(
                "SELECT d.id, d.subscription_id, d.event_type, d.event_id, d.payload,"
                "       d.attempts, s.target_url, s.secret_ciphertext, s.secret_key_version"
                " FROM webhook_delivery d"
                " JOIN webhook_subscription s ON s.id = d.subscription_id"
                " WHERE d.state IN ('pending','failed')"
                "   AND d.next_attempt_at <= :now"
                "   AND s.active AND s.disabled_at IS NULL"
                " ORDER BY d.next_attempt_at"
                " LIMIT :limit"
            ),
            {"now": moment, "limit": limit},
        )
        .mappings()
        .all()
    )

    for row in due:
        result.attempted += 1
        attempt = int(row["attempts"]) + 1

        try:
            secret = unseal(
                SealedCredential(
                    bytes(row["secret_ciphertext"]), int(row["secret_key_version"]), moment
                ),
                camera_id=str(row["subscription_id"]),
            ).password
        except CredentialError as exc:
            # The secret cannot be read, so no attempt can ever succeed. Dead
            # immediately rather than after six pointless requests.
            _record_attempt(
                session,
                row["id"],
                attempt=attempt,
                status_code=None,
                error=f"subscription secret unreadable: {exc}",
                state="dead",
                next_attempt_at=None,
            )
            result.dead += 1
            continue

        signed = sign(row["payload"], secret, at=moment)
        status_code: int | None = None
        error: str | None = None

        try:
            response = (sender or _send)(
                row["target_url"],
                signed.body,
                signed.headers(row["event_id"], row["event_type"]),
            )
            status_code = int(response)
        except Exception as exc:  # noqa: BLE001 -- a failing receiver is data
            error = f"{type(exc).__name__}: {exc}"[:500]

        if status_code is not None and 200 <= status_code < 300:
            _record_attempt(
                session,
                row["id"],
                attempt=attempt,
                status_code=status_code,
                error=None,
                state="delivered",
                next_attempt_at=None,
            )
            session.execute(
                text("UPDATE webhook_subscription SET consecutive_failures = 0 WHERE id = :id"),
                {"id": row["subscription_id"]},
            )
            result.delivered += 1
            continue

        if error is None:
            error = f"receiver returned HTTP {status_code}"

        if attempt >= MAX_ATTEMPTS:
            _record_attempt(
                session,
                row["id"],
                attempt=attempt,
                status_code=status_code,
                error=error,
                state="dead",
                next_attempt_at=None,
            )
            result.dead += 1
        else:
            _record_attempt(
                session,
                row["id"],
                attempt=attempt,
                status_code=status_code,
                error=error,
                state="failed",
                next_attempt_at=moment + timedelta(seconds=backoff_delay(attempt)),
            )
            result.retrying += 1

        disabled = session.execute(
            text(
                "UPDATE webhook_subscription"
                "   SET consecutive_failures = consecutive_failures + 1,"
                "       disabled_at = CASE WHEN consecutive_failures + 1 >= :threshold"
                "                          THEN now() ELSE disabled_at END,"
                "       disabled_reason = CASE WHEN consecutive_failures + 1 >= :threshold"
                "                              THEN :reason ELSE disabled_reason END"
                " WHERE id = :id AND disabled_at IS NULL"
                " RETURNING disabled_at"
            ),
            {
                "id": row["subscription_id"],
                "threshold": FAILURE_DISABLE_THRESHOLD,
                "reason": (
                    f"{FAILURE_DISABLE_THRESHOLD} consecutive delivery failures; "
                    f"re-enable once the endpoint is healthy"
                ),
            },
        ).scalar()
        if disabled is not None:
            result.subscriptions_disabled += 1
            log.warning(
                "subscription %s disabled after %d consecutive failures",
                row["subscription_id"],
                FAILURE_DISABLE_THRESHOLD,
            )

    session.flush()
    return result


def _send(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST to a subscriber, through the SSRF guard.

    A subscriber URL is supplied by an operator, so it is exactly the user-controlled
    URL the guard exists for -- a webhook pointed at 169.254.169.254 would otherwise
    make the platform fetch its own cloud metadata and POST it onwards.
    """
    import requests

    from services.common.ssrf import reverify_before_connect, validate_uri

    target = validate_uri(url)
    reverify_before_connect(target)
    response = requests.post(
        target.url,
        data=body,
        headers=headers,
        timeout=DELIVERY_TIMEOUT_S,
        allow_redirects=False,
    )
    return int(response.status_code)


def replay(session: Session, delivery_id: uuid.UUID) -> bool:
    """Re-queue a dead delivery by hand. Returns False if it was not dead.

    The attempt log is kept rather than cleared: the point of a replay is usually to
    show that the same payload now succeeds, and losing the history of why it failed
    would remove the evidence that the fix worked.
    """
    from sqlalchemy.engine import CursorResult
    from typing import cast as _cast

    result = _cast(
        CursorResult[Any],
        session.execute(
            text(
                "UPDATE webhook_delivery"
                "   SET state = 'pending', next_attempt_at = now(), attempts = 0,"
                "       last_error = NULL"
                " WHERE id = :id AND state = 'dead'"
            ),
            {"id": delivery_id},
        ),
    )
    session.flush()
    return bool(result.rowcount)


__all__ = [
    "BASE_BACKOFF_S",
    "DELIVERY_TIMEOUT_S",
    "EVENT_ID_HEADER",
    "FAILURE_DISABLE_THRESHOLD",
    "MAX_ATTEMPTS",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "DispatchResult",
    "SignedPayload",
    "WebhookError",
    "backoff_delay",
    "dispatch_due",
    "enqueue",
    "replay",
    "sign",
    "verify",
]
