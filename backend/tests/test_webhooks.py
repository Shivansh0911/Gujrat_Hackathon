"""Webhook signing, retry, dead-lettering and replay.

The signature tests pin three things receivers get wrong, and one thing this platform
could get wrong:

* a re-serialised body must fail verification, which is why `sign` returns the exact
  bytes rather than leaving the caller to encode the dict again;
* the timestamp must be inside the signed material, or a captured request replays
  forever;
* comparison must be constant-time.

The dispatcher tests drive a fake sender, so retry curves and dead-lettering are
exercised without a network and without waiting out a backoff.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.api.webhooks import (
    EVENT_ID_HEADER,
    FAILURE_DISABLE_THRESHOLD,
    MAX_ATTEMPTS,
    SIGNATURE_HEADER,
    SIGNATURE_VERSION,
    TIMESTAMP_HEADER,
    backoff_delay,
    dispatch_due,
    enqueue,
    replay,
    sign,
    verify,
)
from services.common.credentials import CameraCredential, KEY_ENV, seal

SECRET = "a-shared-webhook-secret"
PAYLOAD: dict[str, Any] = {
    "event": "alert.raised",
    "plate": "GJ01AB1234",
    "camera_ref": "cam22",
    "tier": "confirmed",
}


# ------------------------------------------------------------------- signing


def test_a_valid_signature_verifies() -> None:
    signed = sign(PAYLOAD, SECRET)
    assert verify(signed.body, signed.signature, signed.timestamp, SECRET)


def test_the_signature_names_its_scheme() -> None:
    """So changing the algorithm later is unambiguous on the wire, not inferred."""
    assert sign(PAYLOAD, SECRET).signature.startswith(f"{SIGNATURE_VERSION}=")


def test_a_re_serialised_body_does_not_verify() -> None:
    """The single most common webhook integration bug, made impossible on our side.

    `sign` returns the exact bytes to send. A caller that re-encodes the dict produces
    different bytes -- different key order, different separators -- and a signature the
    receiver cannot verify.
    """
    signed = sign(PAYLOAD, SECRET)
    re_encoded = json.dumps(PAYLOAD).encode()
    assert re_encoded != signed.body
    assert not verify(re_encoded, signed.signature, signed.timestamp, SECRET)


def test_the_body_is_deterministic() -> None:
    """A signature has to be reproducible during a dispute."""
    first = sign(PAYLOAD, SECRET, at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = sign(
        dict(reversed(list(PAYLOAD.items()))), SECRET, at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert first.body == second.body
    assert first.signature == second.signature


def test_a_tampered_body_does_not_verify() -> None:
    signed = sign(PAYLOAD, SECRET)
    assert not verify(signed.body + b" ", signed.signature, signed.timestamp, SECRET)


def test_the_wrong_secret_does_not_verify() -> None:
    signed = sign(PAYLOAD, SECRET)
    assert not verify(signed.body, signed.signature, signed.timestamp, "not-the-secret")


def test_an_old_timestamp_is_rejected_even_with_a_valid_signature() -> None:
    """Without binding the timestamp, a captured request replays forever."""
    signed = sign(PAYLOAD, SECRET)
    stale = str(int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()))
    assert not verify(signed.body, signed.signature, stale, SECRET)


def test_a_future_timestamp_is_rejected_too() -> None:
    """Clock skew cuts both ways; the tolerance is symmetric."""
    signed = sign(PAYLOAD, SECRET)
    ahead = str(int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()))
    assert not verify(signed.body, signed.signature, ahead, SECRET)


@pytest.mark.parametrize(
    "signature", ["", "garbage", "v2=abc", "v1=", "sha256=abc"], ids=lambda s: s or "empty"
)
def test_a_malformed_signature_is_refused_without_raising(signature: str) -> None:
    signed = sign(PAYLOAD, SECRET)
    assert verify(signed.body, signature, signed.timestamp, SECRET) is False


def test_a_malformed_timestamp_is_refused_without_raising() -> None:
    signed = sign(PAYLOAD, SECRET)
    assert verify(signed.body, signed.signature, "not-a-number", SECRET) is False


def test_the_headers_carry_what_a_receiver_needs_to_deduplicate() -> None:
    """At-least-once means a receiver WILL see a duplicate; handing them the means to
    notice is part of the contract rather than their problem."""
    headers = sign(PAYLOAD, SECRET).headers("evt_abc", "alert.raised")
    assert headers[EVENT_ID_HEADER] == "evt_abc"
    assert SIGNATURE_HEADER in headers and TIMESTAMP_HEADER in headers


# ------------------------------------------------------------------- backoff


def test_backoff_grows_and_is_capped() -> None:
    from services.api.webhooks import MAX_BACKOFF_S

    assert backoff_delay(1) < backoff_delay(4)
    assert all(backoff_delay(a) <= MAX_BACKOFF_S * 1.25 for a in range(1, 12))


def test_backoff_is_jittered() -> None:
    """A receiver recovering from an outage would otherwise be hit by every queued
    delivery in the same instant -- the retry storm that knocks it straight back over."""
    assert len({round(backoff_delay(3), 4) for _ in range(20)}) > 1


# --------------------------------------------------------------- the dispatcher


def _db_url() -> str | None:
    if os.environ.get("SETU_DATABASE_URL"):
        return os.environ["SETU_DATABASE_URL"]
    from services.common.paths import ENV_FILE

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("SETU_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


_URL = _db_url()
needs_db = pytest.mark.skipif(not _reachable(_URL), reason="deliveries are durable rows")


@pytest.fixture
def estate(monkeypatch: pytest.MonkeyPatch) -> Any:
    import base64
    import secrets as _secrets

    monkeypatch.setenv(KEY_ENV, base64.b64encode(_secrets.token_bytes(32)).decode())

    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    dept = uuid.uuid4()
    subscription = uuid.uuid4()
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Hook Dept')"),
        {"d": dept, "c": f"HOOK_{uuid.uuid4().hex[:8]}"},
    )
    sealed = seal(
        CameraCredential(username="webhook", password=SECRET), camera_id=str(subscription)
    )
    session.execute(
        text(
            "INSERT INTO webhook_subscription (id, name, target_url, secret_ciphertext,"
            " secret_key_version, event_types, department_id, active, created_at,"
            " consecutive_failures)"
            " VALUES (:id, 'Control room', 'https://8.8.8.8/hook', :cipher, :ver,"
            " CAST(:events AS jsonb), :dept, true, now(), 0)"
        ),
        {
            "id": subscription,
            "cipher": sealed.ciphertext,
            "ver": sealed.key_version,
            "events": json.dumps(["alert.raised"]),
            "dept": dept,
        },
    )
    session.flush()

    yield {"session": session, "dept": dept, "subscription": subscription}

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("DELETE FROM webhook_delivery WHERE subscription_id = :s"), {"s": subscription}
    )
    session.execute(text("DELETE FROM webhook_subscription WHERE id = :s"), {"s": subscription})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()
    engine.dispose()


class _Sender:
    """A fake receiver. Returns each scripted status in turn, or raises."""

    def __init__(self, script: list[int | Exception]) -> None:
        self.script = script
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.calls.append((url, body, headers))
        outcome = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _state(session: Session, subscription: uuid.UUID) -> tuple[str, int]:
    row = session.execute(
        text(
            "SELECT state, attempts FROM webhook_delivery WHERE subscription_id = :s"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": subscription},
    ).one()
    return str(row[0]), int(row[1])


@needs_db
def test_an_event_is_queued_for_a_matching_subscription(estate: Any) -> None:
    assert (
        enqueue(
            estate["session"],
            event_type="alert.raised",
            payload=PAYLOAD,
            department_id=estate["dept"],
        )
        == 1
    )


@needs_db
def test_an_unsubscribed_event_type_is_not_queued(estate: Any) -> None:
    assert (
        enqueue(
            estate["session"],
            event_type="detection.created",
            payload=PAYLOAD,
            department_id=estate["dept"],
        )
        == 0
    )


@needs_db
def test_another_departments_event_is_not_queued(estate: Any) -> None:
    """The same boundary RLS enforces on every other read."""
    assert (
        enqueue(
            estate["session"],
            event_type="alert.raised",
            payload=PAYLOAD,
            department_id=uuid.uuid4(),
        )
        == 0
    )


@needs_db
def test_a_successful_delivery_is_marked_delivered(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    sender = _Sender([200])

    result = dispatch_due(session, sender=sender)
    assert result.delivered == 1
    assert _state(session, estate["subscription"]) == ("delivered", 1)

    # The receiver got signed bytes it can verify.
    _, body, headers = sender.calls[0]
    assert verify(body, headers[SIGNATURE_HEADER], headers[TIMESTAMP_HEADER], SECRET)


@needs_db
def test_a_failure_is_retried_rather_than_dropped(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])

    result = dispatch_due(session, sender=_Sender([500]))
    assert result.retrying == 1
    state, attempts = _state(session, estate["subscription"])
    assert state == "failed" and attempts == 1


@needs_db
def test_a_retry_is_not_attempted_before_its_backoff_elapses(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    dispatch_due(session, sender=_Sender([500]))

    # Immediately afterwards nothing is due.
    assert dispatch_due(session, sender=_Sender([200])).attempted == 0
    # Well past the first backoff, it is.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert dispatch_due(session, now=later, sender=_Sender([200])).delivered == 1


@needs_db
def test_a_persistently_failing_delivery_becomes_dead_not_lost(estate: Any) -> None:
    """Kept, visible and replayable. Discarding it would lose the evidence of why."""
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])

    moment = datetime.now(timezone.utc)
    for attempt in range(MAX_ATTEMPTS):
        moment += timedelta(hours=1)
        dispatch_due(session, now=moment, sender=_Sender([503]))

    state, attempts = _state(session, estate["subscription"])
    assert state == "dead"
    assert attempts == MAX_ATTEMPTS


@needs_db
def test_every_attempt_is_logged_not_just_the_last(estate: Any) -> None:
    """ "Delivered" and "delivered on the fourth attempt after two timeouts" are
    different operational facts, and only the second says the endpoint is sick."""
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])

    moment = datetime.now(timezone.utc)
    dispatch_due(session, now=moment, sender=_Sender([500]))
    moment += timedelta(hours=1)
    dispatch_due(session, now=moment, sender=_Sender([200]))

    log = session.execute(
        text(
            "SELECT attempt_log FROM webhook_delivery WHERE subscription_id = :s"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": estate["subscription"]},
    ).scalar()
    assert len(log) == 2
    assert log[0]["status_code"] == 500
    assert log[1]["status_code"] == 200


@needs_db
def test_a_network_error_is_recorded_as_data_not_raised(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])

    result = dispatch_due(session, sender=_Sender([TimeoutError("connection timed out")]))
    assert result.retrying == 1
    error = session.execute(
        text(
            "SELECT last_error FROM webhook_delivery WHERE subscription_id = :s"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": estate["subscription"]},
    ).scalar()
    assert "TimeoutError" in error


@needs_db
def test_a_dead_delivery_can_be_replayed_by_hand(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])

    moment = datetime.now(timezone.utc)
    for _ in range(MAX_ATTEMPTS):
        moment += timedelta(hours=1)
        dispatch_due(session, now=moment, sender=_Sender([503]))
    assert _state(session, estate["subscription"])[0] == "dead"

    delivery_id = session.execute(
        text(
            "SELECT id FROM webhook_delivery WHERE subscription_id = :s"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": estate["subscription"]},
    ).scalar()

    assert replay(session, delivery_id) is True
    assert dispatch_due(session, sender=_Sender([200])).delivered == 1


@needs_db
def test_replaying_a_live_delivery_does_nothing(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    delivery_id = session.execute(
        text("SELECT id FROM webhook_delivery WHERE subscription_id = :s"),
        {"s": estate["subscription"]},
    ).scalar()
    assert replay(session, delivery_id) is False


@needs_db
def test_a_replay_keeps_the_history_of_why_it_failed(estate: Any) -> None:
    """The point of a replay is usually to show the same payload now succeeds."""
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    moment = datetime.now(timezone.utc)
    for _ in range(MAX_ATTEMPTS):
        moment += timedelta(hours=1)
        dispatch_due(session, now=moment, sender=_Sender([503]))

    delivery_id = session.execute(
        text(
            "SELECT id FROM webhook_delivery WHERE subscription_id = :s"
            " ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": estate["subscription"]},
    ).scalar()
    replay(session, delivery_id)

    log = session.execute(
        text("SELECT attempt_log FROM webhook_delivery WHERE id = :id"), {"id": delivery_id}
    ).scalar()
    assert len(log) == MAX_ATTEMPTS, "the failure history must survive a replay"


@needs_db
def test_a_subscription_that_keeps_failing_is_disabled(estate: Any) -> None:
    """Retrying a permanently broken endpoint forever is how a queue becomes a bill."""
    session = estate["session"]
    moment = datetime.now(timezone.utc)

    for _ in range(FAILURE_DISABLE_THRESHOLD):
        enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
        moment += timedelta(hours=1)
        dispatch_due(session, now=moment, sender=_Sender([500]))

    disabled, reason = session.execute(
        text("SELECT disabled_at, disabled_reason FROM webhook_subscription WHERE id = :s"),
        {"s": estate["subscription"]},
    ).one()
    assert disabled is not None
    assert "consecutive delivery failures" in reason

    # And nothing further is attempted for it.
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    assert dispatch_due(session, sender=_Sender([200])).attempted == 0


@needs_db
def test_a_success_clears_the_failure_run(estate: Any) -> None:
    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    dispatch_due(session, sender=_Sender([500]))

    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    dispatch_due(session, sender=_Sender([200]))

    failures = session.execute(
        text("SELECT consecutive_failures FROM webhook_subscription WHERE id = :s"),
        {"s": estate["subscription"]},
    ).scalar()
    assert failures == 0


@needs_db
def test_an_unreadable_secret_dies_immediately_rather_than_retrying(
    estate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No attempt could ever succeed, so six pointless requests would be waste."""
    import base64
    import secrets as _secrets

    session = estate["session"]
    enqueue(session, event_type="alert.raised", payload=PAYLOAD, department_id=estate["dept"])
    # Rotate the master key out from under the stored ciphertext.
    monkeypatch.setenv(KEY_ENV, base64.b64encode(_secrets.token_bytes(32)).decode())

    result = dispatch_due(session, sender=_Sender([200]))
    assert result.dead == 1
    assert _state(session, estate["subscription"])[0] == "dead"
