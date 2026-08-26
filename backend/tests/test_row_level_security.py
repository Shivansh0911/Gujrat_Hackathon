"""Row-level security must isolate departments in the database, not just in the app.

The HLD claims three independent levels of tenant isolation. The value of the third
is that it holds when the other two fail, so these tests deliberately bypass the
scoped accessors entirely and issue raw SQL — the exact thing a hurried query, a
reporting script or a debugging endpoint would do.

If any of these pass without RLS, the isolation claim is application-level only.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker


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
pytestmark = pytest.mark.skipif(
    not _reachable(_URL),
    reason="Postgres not reachable; RLS tests need the docker-compose database",
)


@pytest.fixture
def tenants():
    """Two departments, each with one camera and one detection.

    Committed, then cleaned up. Created with RLS bypassed via admin context, which is
    how the seeding path legitimately works.
    """
    engine = create_engine(_URL, future=True)
    factory = sessionmaker(bind=engine, future=True)
    session: Session = factory()

    dept_a, dept_b = uuid.uuid4(), uuid.uuid4()
    cam_a, cam_b = uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:a, :ca, 'Dept A'), (:b, :cb, 'Dept B')"),
        {"a": dept_a, "b": dept_b, "ca": f"RLSA_{suffix}", "cb": f"RLSB_{suffix}"},
    )
    for cam_id, dept_id, ref in ((cam_a, dept_a, f"rls-a-{suffix}"), (cam_b, dept_b, f"rls-b-{suffix}")):
        session.execute(
            text(
                "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
                " geom_source, status, source_type, archive_mode, ownership_class,"
                " created_at, updated_at)"
                " VALUES (:id, :ref, :ref, '', :dept, 'unset', 'ACTIVE', 'file',"
                " 'departmental', 'GOVERNMENT', now(), now())"
            ),
            {"id": cam_id, "ref": ref, "dept": dept_id},
        )
        session.execute(
            text(
                "INSERT INTO detection (id, camera_id, observed_at_utc, plate_raw,"
                " plate_normalised, corrections, confidence, pts_ms, ingested_at_utc,"
                " clock_confidence)"
                " VALUES (gen_random_uuid(), :cam, now(), :plate, :plate, '[]'::jsonb,"
                " 0.9, 1000, now(), 1.0)"
            ),
            {"cam": cam_id, "plate": f"GJ01{ref[-4:].upper()}"},
        )
    session.commit()

    yield {"session": session, "dept_a": dept_a, "dept_b": dept_b,
           "cam_a": cam_a, "cam_b": cam_b}

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(text("DELETE FROM detection WHERE camera_id IN (:a, :b)"),
                    {"a": cam_a, "b": cam_b})
    session.execute(text("DELETE FROM camera WHERE id IN (:a, :b)"), {"a": cam_a, "b": cam_b})
    session.execute(text("DELETE FROM department WHERE id IN (:a, :b)"),
                    {"a": dept_a, "b": dept_b})
    session.commit()
    session.close()
    engine.dispose()


def _as_department(session: Session, department_id) -> None:
    session.execute(text("SELECT set_config('setu.is_admin', 'off', false)"))
    session.execute(
        text("SELECT set_config('setu.department_id', :d, false)"),
        {"d": str(department_id)},
    )


def _as_admin(session: Session) -> None:
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))


def _visible_cameras(session: Session, ids) -> set:
    rows = session.execute(
        text("SELECT id FROM camera WHERE id IN (:a, :b)"), {"a": ids[0], "b": ids[1]}
    ).scalars().all()
    return set(rows)


# ------------------------------------------------------------------ the core claim


def test_department_a_cannot_read_department_b_cameras(tenants):
    """The claim the HLD makes, tested with raw SQL that bypasses every accessor."""
    s = tenants["session"]
    _as_department(s, tenants["dept_a"])
    visible = _visible_cameras(s, (tenants["cam_a"], tenants["cam_b"]))
    assert tenants["cam_a"] in visible
    assert tenants["cam_b"] not in visible, (
        "department B's camera was readable under department A's context; "
        "row-level security is not isolating tenants"
    )


def test_department_b_cannot_read_department_a_cameras(tenants):
    s = tenants["session"]
    _as_department(s, tenants["dept_b"])
    visible = _visible_cameras(s, (tenants["cam_a"], tenants["cam_b"]))
    assert visible == {tenants["cam_b"]}


def test_detections_are_isolated_through_their_camera(tenants):
    """`detection` has no department column; its policy joins through `camera`."""
    s = tenants["session"]
    _as_department(s, tenants["dept_a"])
    rows = s.execute(
        text("SELECT camera_id FROM detection WHERE camera_id IN (:a, :b)"),
        {"a": tenants["cam_a"], "b": tenants["cam_b"]},
    ).scalars().all()
    assert set(rows) == {tenants["cam_a"]}


def test_aggregate_queries_cannot_leak_counts(tenants):
    """A COUNT must not reveal rows the caller cannot select.

    Aggregates are the classic RLS blind spot: a policy applied only to row output
    still lets `count(*)` disclose how many rows exist.
    """
    s = tenants["session"]
    _as_department(s, tenants["dept_a"])
    n = s.execute(
        text("SELECT count(*) FROM camera WHERE id IN (:a, :b)"),
        {"a": tenants["cam_a"], "b": tenants["cam_b"]},
    ).scalar_one()
    assert n == 1


def test_update_cannot_reach_another_department(tenants):
    """Isolation must cover writes, not only reads."""
    s = tenants["session"]
    _as_department(s, tenants["dept_a"])
    result = s.execute(
        text("UPDATE camera SET name = 'hijacked' WHERE id = :b"), {"b": tenants["cam_b"]}
    )
    assert result.rowcount == 0

    _as_admin(s)
    name = s.execute(
        text("SELECT name FROM camera WHERE id = :b"), {"b": tenants["cam_b"]}
    ).scalar_one()
    assert name != "hijacked"


def test_delete_cannot_reach_another_department(tenants):
    s = tenants["session"]
    _as_department(s, tenants["dept_a"])
    result = s.execute(text("DELETE FROM camera WHERE id = :b"), {"b": tenants["cam_b"]})
    assert result.rowcount == 0
    s.rollback()


def test_admin_context_sees_both_departments(tenants):
    """Admin bypass is an explicit policy branch, not RLS being disabled."""
    s = tenants["session"]
    _as_admin(s)
    visible = _visible_cameras(s, (tenants["cam_a"], tenants["cam_b"]))
    assert visible == {tenants["cam_a"], tenants["cam_b"]}


def test_missing_context_sees_nothing(tenants):
    """Failing to set context must fail closed.

    A connection with no identity matches no policy, so it sees zero rows. The
    opposite default -- unset means unrestricted -- is how RLS deployments leak.
    """
    s = tenants["session"]
    s.execute(text("SELECT set_config('setu.is_admin', 'off', false)"))
    s.execute(text("SELECT set_config('setu.department_id', '', false)"))
    visible = _visible_cameras(s, (tenants["cam_a"], tenants["cam_b"]))
    assert visible == set()


def test_rls_is_forced_for_the_table_owner(tenants):
    """FORCE is required or the owning role bypasses every policy silently."""
    s = tenants["session"]
    _as_admin(s)
    rows = s.execute(
        text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname IN ('camera','site','detection','alert')"
        )
    ).mappings().all()
    assert len(rows) == 4
    for row in rows:
        assert row["relrowsecurity"], f"RLS not enabled on {row['relname']}"
        assert row["relforcerowsecurity"], (
            f"RLS not FORCEd on {row['relname']}; the application role owns these "
            "tables and would bypass every policy"
        )
