"""`vehicle_sighting` must accept a vehicle with no plate, and must still isolate tenants.

Two separate claims are tested here and both matter:

**A sighting does not need a plate.** That is the whole reason the table exists. If a
NULL `plate_detection_id` were ever rejected -- by a constraint, by a NOT NULL added in
a later migration, by an ORM default -- the platform would silently fall back to knowing
about one vehicle per ~3,900 frames on the government estate.

**A sighting is still department-scoped.** `vehicle_sighting` carries no
`department_id`; it reaches the tenant boundary through `camera`, exactly as `detection`
does. A new table is the easiest place to forget RLS, and forgetting it here would be
worse than on `detection`, because sightings exist for *every* vehicle rather than only
the ones whose plate was legible.

These tests issue raw SQL rather than going through the scoped accessors, for the reason
`test_row_level_security.py` gives: the value of database-level isolation is that it
holds when the application layer does not.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from services.analytics.model_ids import APPEARANCE_DIM, APPEARANCE_KIND


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
    reason="Postgres not reachable; schema tests need the docker-compose database",
)

_ZERO_VECTOR = "[" + ",".join(["0.1"] * APPEARANCE_DIM) + "]"

#: The bbox travels as a bind parameter rather than an inline literal. An inline
#: `'{"x1":10,...}'::jsonb` reads as SQL containing `:10`, which SQLAlchemy's `text()`
#: parses as a bind parameter named "10" and then refuses to execute for want of a
#: value -- a confusing failure that looks like a schema problem and is not one.
_BBOX = '{"x1": 10, "y1": 20, "x2": 110, "y2": 100}'

_INSERT = text(
    """
    INSERT INTO vehicle_sighting (
        id, camera_id, observed_at_utc, vehicle_bbox, vehicle_class,
        vehicle_class_confidence, track_id, plate_detection_id, plate_detection_at,
        appearance_vector, appearance_kind, aspect_ratio, rel_area, rel_cx, rel_cy,
        quality_score, pts_ms, clock_confidence, ingested_at_utc
    ) VALUES (
        :id, :cam, now(), CAST(:bbox AS jsonb), :cls,
        :cls_conf, :track, :plate_id, :plate_at,
        CAST(:vec AS vector), :kind, 1.25, 0.04, 0.5, 0.6,
        :quality, 1000, 1.0, now()
    )
    """
)


@pytest.fixture
def tenants() -> Any:
    """Two departments, each with one camera. No detections: that is the point."""
    engine = create_engine(_URL or "", future=True)
    factory = sessionmaker(bind=engine, future=True)
    session: Session = factory()

    dept_a, dept_b = uuid.uuid4(), uuid.uuid4()
    cam_a, cam_b = uuid.uuid4(), uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text(
            "INSERT INTO department (id, code, name)"
            " VALUES (:a, :ca, 'Sighting Dept A'), (:b, :cb, 'Sighting Dept B')"
        ),
        {"a": dept_a, "b": dept_b, "ca": f"VSA_{suffix}", "cb": f"VSB_{suffix}"},
    )
    for cam_id, dept_id, ref in (
        (cam_a, dept_a, f"vs-a-{suffix}"),
        (cam_b, dept_b, f"vs-b-{suffix}"),
    ):
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
    session.commit()

    yield {"session": session, "dept_a": dept_a, "dept_b": dept_b, "cam_a": cam_a, "cam_b": cam_b}

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("DELETE FROM vehicle_sighting WHERE camera_id IN (:a, :b)"), {"a": cam_a, "b": cam_b}
    )
    session.execute(text("DELETE FROM camera WHERE id IN (:a, :b)"), {"a": cam_a, "b": cam_b})
    session.execute(text("DELETE FROM department WHERE id IN (:a, :b)"), {"a": dept_a, "b": dept_b})
    session.commit()
    session.close()
    engine.dispose()


def _insert(session: Session, cam_id: uuid.UUID, **over: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    params: dict[str, Any] = {
        "id": row_id,
        "cam": cam_id,
        "bbox": _BBOX,
        "cls": "car",
        "cls_conf": 0.8,
        "track": "t1",
        "plate_id": None,
        "plate_at": None,
        "vec": _ZERO_VECTOR,
        "kind": APPEARANCE_KIND,
        "quality": 0.6,
    }
    params.update(over)
    session.execute(_INSERT, params)
    return row_id


def _as_department(session: Session, department_id: uuid.UUID) -> None:
    session.execute(text("SELECT set_config('setu.is_admin', 'off', false)"))
    session.execute(
        text("SELECT set_config('setu.department_id', :d, false)"), {"d": str(department_id)}
    )


def _as_admin(session: Session) -> None:
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))


# ------------------------------------------------------ the point of the whole table


def test_a_sighting_is_accepted_with_no_plate_reference(tenants: Any) -> None:
    """A vehicle with an unreadable plate must still be recordable.

    If this ever fails, the platform is back to only knowing about vehicles whose
    registration happened to be legible -- one per ~3,900 frames on the government
    estate (DISCOVERY finding 22).
    """
    session: Session = tenants["session"]
    _as_admin(session)
    row_id = _insert(session, tenants["cam_a"], plate_id=None, plate_at=None)
    session.flush()

    got = session.execute(
        text(
            "SELECT plate_detection_id, appearance_kind, vehicle_class"
            " FROM vehicle_sighting WHERE id = :id"
        ),
        {"id": row_id},
    ).one()
    assert got[0] is None
    assert got[1] == APPEARANCE_KIND
    assert got[2] == "car"


def test_a_sighting_is_accepted_with_no_fingerprint_either(tenants: Any) -> None:
    """A crop below the quality floor yields no vector, and the sighting still stands.

    It continues to count for flow, dwell and counting analytics, none of which need an
    appearance fingerprint.
    """
    session: Session = tenants["session"]
    _as_admin(session)
    row_id = _insert(session, tenants["cam_a"], vec=None, kind=None, quality=0.05)
    session.flush()
    assert session.execute(
        text("SELECT appearance_vector IS NULL FROM vehicle_sighting WHERE id = :id"),
        {"id": row_id},
    ).scalar()


# ------------------------------------------------------------- schema drift guards


def test_stored_vector_width_matches_the_descriptor(tenants: Any) -> None:
    """The migration hard-codes the column width; the descriptor computes its own.

    They are intentionally declared in one place (`model_ids.APPEARANCE_DIM`) and
    asserted against the live column here, because a mismatch would otherwise surface
    as a write failure on the first real ingest rather than in CI.
    """
    session: Session = tenants["session"]
    _as_admin(session)
    declared = session.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
            " WHERE attrelid = 'public.vehicle_sighting'::regclass"
            "   AND attname = 'appearance_vector'"
        )
    ).scalar()
    assert declared == f"vector({APPEARANCE_DIM})"

    from services.analytics.appearance import DESCRIPTOR_DIM

    assert DESCRIPTOR_DIM == APPEARANCE_DIM


def test_a_vector_of_the_wrong_width_is_refused(tenants: Any) -> None:
    """pgvector enforces the dimension; relying on that is cheaper than checking twice."""
    session: Session = tenants["session"]
    _as_admin(session)
    with pytest.raises((DBAPIError, IntegrityError)):
        _insert(session, tenants["cam_a"], vec="[0.1,0.2,0.3]")
        session.flush()
    session.rollback()


def test_the_table_is_a_hypertable_when_timescale_is_present(tenants: Any) -> None:
    """Partitioning is an optimisation, so this asserts only the conditional."""
    session: Session = tenants["session"]
    _as_admin(session)
    has_timescale = session.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    ).scalar()
    if not has_timescale:
        pytest.skip("timescaledb absent; 0007 leaves a plain table by design")
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM timescaledb_information.hypertables"
                " WHERE hypertable_name = 'vehicle_sighting'"
            )
        ).scalar()
        == 1
    )


# ------------------------------------------------------------- pairing constraints


def test_half_a_plate_reference_is_refused(tenants: Any) -> None:
    """`detection`'s key is composite; half of it resolves to nothing."""
    session: Session = tenants["session"]
    _as_admin(session)
    with pytest.raises(IntegrityError, match="ck_sighting_plate_ref_complete"):
        _insert(session, tenants["cam_a"], plate_id=uuid.uuid4(), plate_at=None)
        session.flush()
    session.rollback()


def test_a_vector_without_its_kind_is_refused(tenants: Any) -> None:
    """A vector whose layout is unknown cannot be compared with anything."""
    session: Session = tenants["session"]
    _as_admin(session)
    with pytest.raises(IntegrityError, match="ck_sighting_vector_kind_paired"):
        _insert(session, tenants["cam_a"], kind=None)
        session.flush()
    session.rollback()


def test_quality_outside_zero_to_one_is_refused(tenants: Any) -> None:
    session: Session = tenants["session"]
    _as_admin(session)
    with pytest.raises(IntegrityError, match="ck_sighting_quality"):
        _insert(session, tenants["cam_a"], quality=1.4)
        session.flush()
    session.rollback()


# --------------------------------------------------------- row-level security


def test_a_department_sees_only_its_own_sightings(tenants: Any) -> None:
    session: Session = tenants["session"]
    _as_admin(session)
    id_a = _insert(session, tenants["cam_a"], track="a-1")
    id_b = _insert(session, tenants["cam_b"], track="b-1")
    session.commit()

    _as_department(session, tenants["dept_a"])
    visible = {
        r[0]
        for r in session.execute(
            text("SELECT id FROM vehicle_sighting WHERE id IN (:a, :b)"), {"a": id_a, "b": id_b}
        )
    }
    assert visible == {id_a}, "department A must not see department B's sightings"

    _as_department(session, tenants["dept_b"])
    visible = {
        r[0]
        for r in session.execute(
            text("SELECT id FROM vehicle_sighting WHERE id IN (:a, :b)"), {"a": id_a, "b": id_b}
        )
    }
    assert visible == {id_b}
    session.rollback()


def test_no_session_context_sees_nothing(tenants: Any) -> None:
    """Forgetting to set the identity must fail closed, not open."""
    session: Session = tenants["session"]
    _as_admin(session)
    _insert(session, tenants["cam_a"])
    session.commit()

    session.execute(text("SELECT set_config('setu.is_admin', 'off', false)"))
    session.execute(text("SELECT set_config('setu.department_id', '', false)"))
    assert (
        session.execute(
            text("SELECT count(*) FROM vehicle_sighting WHERE camera_id = :c"),
            {"c": tenants["cam_a"]},
        ).scalar()
        == 0
    )
    session.rollback()


def test_a_department_cannot_write_a_sighting_onto_another_departments_camera(
    tenants: Any,
) -> None:
    """WITH CHECK, not just USING: isolation that only filters reads is half a control."""
    session: Session = tenants["session"]
    _as_department(session, tenants["dept_a"])
    with pytest.raises((IntegrityError, DBAPIError)):
        _insert(session, tenants["cam_b"])
        session.flush()
    session.rollback()
