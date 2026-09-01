"""Concurrent appends must still produce a verifiable chain.

`test_audit_chain.py` writes entries one after another, which is the case that was never
in doubt. The case that actually broke a deployed ledger is two writers appending at the
same instant: both read the same tail, both hash it as their predecessor, and the
verifier then reports "an entry was inserted, removed or reordered" on a ledger nobody
tampered with.

That is not hypothetical. Seven links broke on the live instance when a container
restart loop ran the seeding job while the API was serving requests. The chain was
doing its job; the writer was not.

So this test does the thing that broke it -- real threads, real sessions, real
transactions, appending at once -- and then verifies the chain.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from services.api import audit
from services.registry.models import AuditEntry

WRITERS = 8
PER_WRITER = 4


def _read_db_url() -> str | None:
    url = os.environ.get("SETU_MIGRATION_DATABASE_URL")
    if url:
        return url
    from services.common.paths import ENV_FILE

    if not ENV_FILE.exists():
        return None
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for key in ("SETU_MIGRATION_DATABASE_URL", "SETU_DATABASE_URL"):
        for line in lines:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


_URL = _read_db_url()


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


pytestmark = pytest.mark.skipif(
    not _reachable(_URL),
    reason="Postgres not reachable; the concurrency test needs the real database",
)


@pytest.fixture
def schema():
    """An isolated schema, pinned on every pooled connection.

    `SET search_path` on a session is not enough here and the first version of this test
    proved it: a session returns its connection to the pool on commit, and the next
    statement can be handed a different one with the default search_path. Four of
    thirty-two appends landed outside the test schema, so the count came back 28. The
    option goes on the connection string instead, where every connection the pool hands
    out carries it.
    """
    name = f"audit_conc_{uuid.uuid4().hex[:8]}"
    admin = create_engine(_URL, future=True)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))

    engine = create_engine(
        _URL,
        future=True,
        pool_size=WRITERS + 2,
        max_overflow=4,
        connect_args={"options": f"-csearch_path={name}"},
    )
    with engine.begin() as conn:
        AuditEntry.__table__.create(conn)
    try:
        yield engine, name
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


def test_concurrent_appends_leave_the_chain_verifiable(schema) -> None:
    engine, name = schema
    factory = sessionmaker(bind=engine, future=True)
    errors: list[BaseException] = []
    # Line the threads up so the appends genuinely overlap. Without this they tend to
    # serialise by accident and the test passes for the wrong reason.
    barrier = threading.Barrier(WRITERS)

    def writer(n: int) -> None:
        try:
            sess = factory()
            barrier.wait(timeout=30)
            for i in range(PER_WRITER):
                audit.append(
                    sess,
                    action="CONCURRENCY_PROBE",
                    subject_type="test",
                    subject_id=f"w{n}-{i}",
                    actor_id=f"writer-{n}",
                    actor_role="admin",
                    purpose="concurrent append probe",
                )
                sess.commit()
            sess.close()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    assert not errors, f"a writer raised: {errors[0]!r}"

    sess = factory()
    try:
        report = audit.verify_chain(sess)
    finally:
        sess.close()

    assert report["entries_checked"] == WRITERS * PER_WRITER
    assert report["valid"], (
        f"{len(report['breaks'])} broken link(s) after {WRITERS} concurrent writers: "
        f"{report['breaks'][:3]}"
    )
