"""The audit chain must detect tampering, not merely claim to.

These tests run against the real Postgres in docker-compose, because the guarantee
depends on how rows are actually stored and ordered -- an in-memory substitute would
be testing a different thing. They are skipped, loudly, when the database is absent.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.api import audit
from services.registry.models import AuditEntry

DB_URL = os.environ.get("SETU_DATABASE_URL")


def _read_db_url() -> str | None:
    """Database URL from the environment, falling back to the project .env.

    The .env location comes from services.common.paths rather than being derived
    here: when the backend moved into backend/, a locally-computed path silently
    pointed at a file that no longer existed and every audit test skipped instead of
    failing -- a green run that had verified nothing.
    """
    if DB_URL:
        return DB_URL
    from services.common.paths import ENV_FILE

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("SETU_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


_URL = _read_db_url()


def _reachable(url: str | None) -> bool:
    """A configured-but-unreachable database must skip, not error in a fixture.

    Docker is not always running on a developer machine. Erroring six fixtures with a
    socket traceback hides which tests genuinely failed; skipping with a clear reason
    keeps the suite readable.
    """
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(_URL),
    reason="Postgres not reachable; audit chain tests need the docker-compose database",
)


@pytest.fixture
def session() -> Session:
    """An isolated schema per test, so tests cannot see each other's chain."""
    engine = create_engine(_URL, future=True)
    schema = f"audit_test_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'SET search_path TO "{schema}"'))
        AuditEntry.__table__.create(conn)

    factory = sessionmaker(bind=engine, future=True)
    sess = factory()
    sess.execute(text(f'SET search_path TO "{schema}"'))
    try:
        yield sess
    finally:
        sess.close()
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def _append(sess: Session, action: str, subject_id: str = "cam-1") -> AuditEntry:
    entry = audit.append(
        sess, action=action, subject_type="camera", subject_id=subject_id,
        actor_id="tester", actor_role="admin", detail={"note": action},
    )
    sess.commit()
    return entry


def test_first_entry_links_to_genesis(session):
    entry = _append(session, "CAMERA_ADDED")
    assert entry.prev_hash == audit.GENESIS_HASH
    assert audit.verify_chain(session)["valid"] is True


def test_each_entry_links_to_its_predecessor(session):
    a = _append(session, "CAMERA_ADDED")
    b = _append(session, "CAMERA_GEOM_UPDATED")
    c = _append(session, "VIEW_STREAM")
    assert b.prev_hash == a.entry_hash
    assert c.prev_hash == b.entry_hash

    result = audit.verify_chain(session)
    assert result["valid"] is True
    assert result["entries_checked"] == 3
    assert result["head_hash"] == c.entry_hash.hex()


def test_mutating_a_historical_row_is_detected(session):
    """The core claim: changing a past record invalidates the chain."""
    _append(session, "CAMERA_ADDED")
    victim = _append(session, "CAMERA_GEOM_UPDATED")
    _append(session, "VIEW_STREAM")
    assert audit.verify_chain(session)["valid"] is True

    # Someone edits history directly in the database, leaving hashes untouched --
    # exactly what an insider with database access would do.
    session.execute(
        text("UPDATE audit_entry SET detail = :d WHERE seq = :s"),
        {"d": '{"note": "TAMPERED"}', "s": victim.seq},
    )
    session.commit()
    session.expire_all()

    result = audit.verify_chain(session)
    assert result["valid"] is False
    kinds = {b["kind"] for b in result["breaks"]}
    assert "content_modified" in kinds
    # The break is attributed to the row that was altered.
    assert any(b["seq"] == victim.seq for b in result["breaks"])


def test_changing_the_actor_is_detected(session):
    """Rewriting who did something must break the chain, not just what was done."""
    _append(session, "CAMERA_ADDED")
    victim = _append(session, "EXPORT_EVIDENCE")
    session.execute(
        text("UPDATE audit_entry SET actor_id = 'someone_else' WHERE seq = :s"),
        {"s": victim.seq},
    )
    session.commit()
    session.expire_all()
    assert audit.verify_chain(session)["valid"] is False


def test_deleting_an_entry_is_detected(session):
    _append(session, "CAMERA_ADDED")
    victim = _append(session, "CAMERA_GEOM_UPDATED")
    _append(session, "VIEW_STREAM")

    session.execute(text("DELETE FROM audit_entry WHERE seq = :s"), {"s": victim.seq})
    session.commit()
    session.expire_all()

    result = audit.verify_chain(session)
    assert result["valid"] is False
    assert "broken_link" in {b["kind"] for b in result["breaks"]}


def test_empty_chain_is_valid(session):
    result = audit.verify_chain(session)
    assert result["valid"] is True
    assert result["entries_checked"] == 0
    assert result["head_hash"] is None


def test_canonical_json_is_order_independent():
    # Two dicts with the same content in different insertion orders must hash
    # identically, or verification fails on rows nobody touched.
    a = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
    b = {"a": {"x": [3, 4], "y": 2}, "b": 1}
    assert audit.canonical_json(a) == audit.canonical_json(b)


def test_hash_depends_on_predecessor():
    """Identical content at a different chain position must hash differently."""
    payload = {"seq": 1, "action": "X"}
    h1 = audit.compute_hash(audit.GENESIS_HASH, payload)
    h2 = audit.compute_hash(b"\x11" * 32, payload)
    assert h1 != h2
