"""A retry must get the original answer, not an error and not a second write.

The test that matters most is `test_a_retry_replays_the_original_response`, because the
naive implementation of this feature -- reject duplicate keys with a 409 -- leaves the
client exactly where it started: it still does not know whether the first attempt
worked, now with an error attached.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.api.errors import ApiError, ErrorCode
from services.api.idempotency import (
    IDEMPOTENCY_HEADER,
    MAX_KEY_LENGTH,
    abandon,
    begin,
    complete,
    hash_request,
    purge_expired,
)

BODY = {"cameras": [{"ref": "cam01", "name": "Chimanbhai Bridge"}]}
ENDPOINT = "POST /cameras/bulk-import"
ACTOR = "apikey:abcd1234"


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
pytestmark = pytest.mark.skipif(not _reachable(_URL), reason="idempotency records are durable rows")


@pytest.fixture
def session() -> Session:
    engine = create_engine(_URL or "", future=True)
    s: Session = sessionmaker(bind=engine, future=True)()
    s.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    yield s
    s.rollback()
    s.close()
    engine.dispose()


@pytest.fixture
def key() -> str:
    return f"test-{uuid.uuid4()}"


# ----------------------------------------------------------------- the main case


def test_a_first_request_is_told_to_proceed(session: Session, key: str) -> None:
    assert begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY) is None


def test_a_retry_replays_the_original_response(session: Session, key: str) -> None:
    """The client retried because it never saw the first answer. Give it that answer.

    Returning a 409 instead would leave it exactly where it started -- not knowing
    whether the first attempt worked -- with an error on top.
    """
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session,
        key=key,
        endpoint=ENDPOINT,
        actor=ACTOR,
        status_code=201,
        response_body={"imported": 1, "camera_ids": ["abc"]},
    )

    replay = begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    assert replay is not None
    assert replay.status_code == 201
    assert replay.body == {"imported": 1, "camera_ids": ["abc"]}


def test_a_replay_is_byte_for_byte_the_original(session: Session, key: str) -> None:
    original = {"imported": 3, "rejected": [{"row": 2, "reason": "no coordinate"}]}
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=200, response_body=original
    )

    replay = begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    assert replay is not None and replay.body == original


# ------------------------------------------------------------------- the scope


def test_the_same_key_from_a_different_caller_is_a_different_request(
    session: Session, key: str
) -> None:
    """Keys are client-chosen; two integrations will eventually pick the same UUID."""
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=201, response_body={"a": 1}
    )

    assert (
        begin(session, key=key, endpoint=ENDPOINT, actor="apikey:other", request_body=BODY) is None
    )


def test_the_same_key_against_a_different_endpoint_is_a_different_request(
    session: Session, key: str
) -> None:
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=201, response_body={"a": 1}
    )

    assert (
        begin(
            session,
            key=key,
            endpoint="POST /watchlist/bulk-import",
            actor=ACTOR,
            request_body=BODY,
        )
        is None
    )


# ------------------------------------------------------------------ the refusals


def test_the_same_key_with_a_different_body_is_refused(session: Session, key: str) -> None:
    """A client bug. Replaying the first response would hide the bug and the fact that
    their second request did nothing."""
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=201, response_body={"a": 1}
    )

    with pytest.raises(ApiError) as exc:
        begin(
            session,
            key=key,
            endpoint=ENDPOINT,
            actor=ACTOR,
            request_body={"cameras": [{"ref": "cam99", "name": "Somewhere else"}]},
        )
    assert exc.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED


def test_a_request_still_in_flight_is_told_to_wait(session: Session, key: str) -> None:
    """A client whose timeout is shorter than the server's work. Common, not exotic."""
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)

    with pytest.raises(ApiError) as exc:
        begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    assert exc.value.code is ErrorCode.CONFLICT
    assert exc.value.retry_after_s is not None
    assert "in progress" in exc.value.message


@pytest.mark.parametrize("bad", ["", "x" * (MAX_KEY_LENGTH + 1)], ids=["empty", "oversized"])
def test_a_malformed_key_is_refused(session: Session, bad: str) -> None:
    with pytest.raises(ApiError) as exc:
        begin(session, key=bad, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    assert exc.value.code is ErrorCode.VALIDATION_FAILED
    assert IDEMPOTENCY_HEADER in exc.value.message


# ------------------------------------------------------------------- failures


def test_an_abandoned_claim_lets_the_client_retry_properly(session: Session, key: str) -> None:
    """Without this, a request that raised holds its key for 24 hours and every retry
    is told "still in progress"."""
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    abandon(session, key=key, endpoint=ENDPOINT, actor=ACTOR)

    assert begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY) is None


def test_abandoning_does_not_erase_a_completed_record(session: Session, key: str) -> None:
    """A successful response must stay replayable even if cleanup runs over it."""
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=201, response_body={"a": 1}
    )
    abandon(session, key=key, endpoint=ENDPOINT, actor=ACTOR)

    replay = begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    assert replay is not None and replay.status_code == 201


# ------------------------------------------------------------------- expiry


def test_an_expired_key_can_be_used_again(session: Session, key: str) -> None:
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    complete(
        session, key=key, endpoint=ENDPOINT, actor=ACTOR, status_code=201, response_body={"a": 1}
    )
    session.execute(
        text("UPDATE idempotency_key SET expires_at = now() - interval '1 hour' WHERE key = :k"),
        {"k": key},
    )

    assert begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY) is None


def test_expired_records_are_purgeable(session: Session, key: str) -> None:
    begin(session, key=key, endpoint=ENDPOINT, actor=ACTOR, request_body=BODY)
    session.execute(
        text("UPDATE idempotency_key SET expires_at = now() - interval '1 hour' WHERE key = :k"),
        {"k": key},
    )
    assert purge_expired(session) >= 1


# --------------------------------------------------------------- the body hash


def test_the_hash_is_stable_across_key_order() -> None:
    """The same logical request must hash the same way whatever the client's encoder did."""
    assert hash_request({"a": 1, "b": 2}) == hash_request({"b": 2, "a": 1})


def test_the_hash_distinguishes_different_bodies() -> None:
    assert hash_request({"a": 1}) != hash_request({"a": 2})


def test_the_hash_handles_values_json_cannot_encode_natively() -> None:
    """Datetimes reach this from request models and must not raise."""
    hash_request({"at": datetime(2026, 1, 1, tzinfo=timezone.utc), "d": timedelta(hours=1)})


def test_the_hash_does_not_collide_on_nesting() -> None:
    assert hash_request({"a": {"b": 1}}) != hash_request({"a": {"b": "1"}})


def test_a_list_body_is_hashable() -> None:
    """Bulk endpoints post arrays, not objects."""
    assert hash_request([{"ref": "cam01"}]) != hash_request([{"ref": "cam02"}])


def test_the_retention_window_covers_a_sane_retry() -> None:
    from services.api.idempotency import RETENTION

    assert RETENTION >= timedelta(hours=1)
