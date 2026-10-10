"""Traffic counted from vehicles seen rather than registrations read.

The point of this module is the gap between the two numbers, so the tests are mostly
about that: a vehicle with no readable plate must be counted here and must remain
invisible to `/vehicle-counts`, and the per-hour means must describe the period a
camera was actually watched rather than the period that was asked about.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics.traffic import traffic_report


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
    not _reachable(_URL), reason="Postgres not reachable; the aggregate is SQL"
)

NOW = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
WINDOW = {"from_": NOW - timedelta(hours=1), "to": NOW + timedelta(hours=12)}


@pytest.fixture
def estate() -> Any:
    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    refs = [f"TR-{n}-{suffix}" for n in ("A", "B")]
    cams = {ref: uuid.uuid4() for ref in refs}

    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Traffic Dept')"),
        {"d": dept, "c": f"TR_{suffix}"},
    )
    for index, ref in enumerate(refs):
        session.execute(
            text(
                "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
                " geom, geom_source, confidence_radius_m, status, source_type,"
                " archive_mode, ownership_class, created_at, updated_at)"
                " VALUES (:id, :ref, :ref, '', :dept,"
                " ST_SetSRID(ST_MakePoint(:lon, 23.0225), 4326)::geography,"
                " 'approximate', 300, 'ACTIVE', 'file', 'departmental', 'GOVERNMENT',"
                " now(), now())"
            ),
            {"id": cams[ref], "ref": ref, "dept": dept, "lon": 72.5714 + index * 0.02},
        )
    session.commit()

    yield {"session": session, "refs": refs, "cams": cams}

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    ids = [str(v) for v in cams.values()]
    session.execute(
        text("DELETE FROM vehicle_sighting WHERE camera_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": ids},
    )
    session.execute(
        text("DELETE FROM detection WHERE camera_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
    )
    session.execute(text("DELETE FROM camera WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()
    engine.dispose()


def _sighting(
    session: Session, cam: uuid.UUID, at: datetime, *, cls: str = "car", rel_area: float = 0.05
) -> None:
    session.execute(
        text(
            "INSERT INTO vehicle_sighting (id, camera_id, observed_at_utc, vehicle_class,"
            " vehicle_class_confidence, vehicle_bbox, rel_area, rel_cx, rel_cy,"
            " aspect_ratio, quality_score, frames_seen, track_id, pts_ms, last_pts_ms,"
            " clock_confidence, ingested_at_utc, track_displacement_px)"
            " VALUES (gen_random_uuid(), :cam, :at, :cls, 0.9,"
            # The bbox is bound, not inlined. A JSON literal in the statement puts
            # `:0` in front of SQLAlchemy's parameter parser, which reads it as a bind
            # name and fails before Postgres ever sees the row.
            " CAST(:bbox AS jsonb), :rel, 0.5, 0.5, 1.25, 0.8,"
            " 5, :track, 1000, 2000, 1.0, now(), 40)"
        ),
        {
            "cam": cam,
            "at": at,
            "cls": cls,
            "rel": rel_area,
            "track": uuid.uuid4().hex[:12],
            "bbox": '{"x1":0,"y1":0,"x2":100,"y2":80}',
        },
    )


def test_a_vehicle_with_no_readable_plate_is_still_counted(estate: Any) -> None:
    """The whole reason this module exists, as a test.

    Not one detection row is written here -- no plate was ever read -- and the traffic
    report must still see the vehicles.
    """
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    for i in range(7):
        _sighting(session, cams[refs[0]], NOW + timedelta(minutes=5 * i))
    session.commit()

    report = traffic_report(session, bucket="hour", camera_ids=[cams[refs[0]]], **WINDOW)
    assert report.total_vehicles == 7
    assert report.cameras_reporting == 1

    reads = session.execute(
        text("SELECT count(*) FROM detection WHERE camera_id = :c"), {"c": cams[refs[0]]}
    ).scalar()
    assert reads == 0, "the point is that these vehicles produced no plate reads at all"


def test_the_class_mix_is_counted_by_the_values_actually_stored(estate: Any) -> None:
    """`VehicleClass` is a lower-case StrEnum; matching upper case silently buckets
    every pass as unknown, which reads as a broken detector rather than a typo."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    for cls, n in (("car", 4), ("bus", 3), ("motorcycle", 2), ("truck", 1)):
        for i in range(n):
            _sighting(session, cams[refs[0]], NOW + timedelta(minutes=i), cls=cls)
    session.commit()

    report = traffic_report(session, bucket="hour", camera_ids=[cams[refs[0]]], **WINDOW)
    assert report.classes == {
        "car": 4,
        "bus": 3,
        "motorcycle": 2,
        "truck": 1,
        "unknown": 0,
    }


def test_per_hour_describes_the_hours_a_camera_reported(estate: Any) -> None:
    """A camera watched for one hour of twelve is barely watched, not quiet.

    Dividing by the requested window would report ten passes in one hour as 0.8/h,
    which is the kind of average that makes a busy junction look idle.
    """
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    for i in range(10):
        _sighting(session, cams[refs[0]], NOW + timedelta(minutes=i))
    session.commit()

    report = traffic_report(session, bucket="hour", camera_ids=[cams[refs[0]]], **WINDOW)
    assert report.by_camera[0].per_hour == 10.0


def test_the_peak_bucket_is_reported(estate: Any) -> None:
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    for i in range(2):
        _sighting(session, cams[refs[0]], NOW + timedelta(minutes=i))
    for i in range(6):
        _sighting(session, cams[refs[0]], NOW + timedelta(hours=3, minutes=i))
    session.commit()

    report = traffic_report(session, bucket="hour", camera_ids=[cams[refs[0]]], **WINDOW)
    assert report.peak_vehicles == 6
    assert report.peak_bucket_start_utc is not None
    assert report.peak_bucket_start_utc.hour == (NOW + timedelta(hours=3)).hour


def test_cameras_outside_the_actors_scope_contribute_nothing(estate: Any) -> None:
    """An aggregate is a way to leak activity without ever naming the camera."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    for i in range(5):
        _sighting(session, cams[refs[1]], NOW + timedelta(minutes=i))
    session.commit()

    report = traffic_report(session, bucket="hour", camera_ids=[cams[refs[0]]], **WINDOW)
    assert report.total_vehicles == 0
    assert report.by_camera == []


def test_an_empty_scope_returns_zero_rather_than_the_whole_estate(estate: Any) -> None:
    report = traffic_report(estate["session"], bucket="hour", camera_ids=[], **WINDOW)
    assert report.total_vehicles == 0
    assert report.cameras_reporting == 0
    assert report.caveat, "the caveat must travel even when the result is empty"
