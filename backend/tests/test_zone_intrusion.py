"""Zone intrusion: a hit, a miss, and a vehicle that lingers.

These run against real PostGIS because the containment test *is* PostGIS. Reimplementing
point-in-polygon in Python to make the test run without a database would be testing a
different function from the one that ships.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics import zones
from services.registry.models import Detection


def _db_url() -> str | None:
    url = os.environ.get("SETU_MIGRATION_DATABASE_URL")
    if url:
        return url
    from services.common.paths import ENV_FILE

    if not ENV_FILE.exists():
        return None
    lines = io.open(ENV_FILE, encoding="utf-8").read().splitlines()
    for key in ("SETU_MIGRATION_DATABASE_URL", "SETU_DATABASE_URL"):
        for line in lines:
            if line.startswith(f"{key}="):
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
    not _reachable(_URL), reason="Postgres not reachable; zone tests need real PostGIS"
)

#: A square in the middle of a 1920x1080 frame.
ZONE_RING = [(800, 400), (1200, 400), (1200, 700), (800, 700), (800, 400)]


@pytest.fixture
def fixture_camera():
    """One department, one camera, one zone. Committed, then cleaned up."""
    engine = create_engine(_URL, future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    suffix = uuid.uuid4().hex[:8]
    dept, cam, zone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Zone Test')"),
        {"d": dept, "c": f"ZON_{suffix}"},
    )
    session.execute(
        text(
            "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
            " geom_source, status, source_type, archive_mode, ownership_class,"
            " created_at, updated_at)"
            " VALUES (:id, :ref, :ref, '', :dept, 'unset', 'ACTIVE', 'file',"
            " 'departmental', 'GOVERNMENT', now(), now())"
        ),
        {"id": cam, "ref": f"zone-{suffix}", "dept": dept},
    )
    ring = ", ".join(f"{x} {y}" for x, y in ZONE_RING)
    session.execute(
        text(
            "INSERT INTO camera_zone (id, camera_id, name, polygon, reference_width,"
            " reference_height, active, created_at)"
            f" VALUES (:id, :cam, 'gate', ST_SetSRID(ST_GeomFromText('POLYGON(({ring}))'), 0),"
            " 1920, 1080, true, now())"
        ),
        {"id": zone, "cam": cam},
    )
    session.commit()

    yield {"session": session, "camera_id": cam, "zone_id": zone}

    # Discard anything a test left pending. Alerts are added to the session but never
    # committed, and without this they flush during cleanup -- after the camera row has
    # gone -- and fail on the foreign key rather than on anything the test cared about.
    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(text("DELETE FROM alert WHERE camera_id = :c"), {"c": cam})
    session.execute(text("DELETE FROM detection WHERE camera_id = :c"), {"c": cam})
    session.execute(text("DELETE FROM camera_zone WHERE camera_id = :c"), {"c": cam})
    session.execute(text("DELETE FROM camera WHERE id = :c"), {"c": cam})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()


def _detection(session: Session, camera_id, bbox, *, at: datetime, plate="GJ01AB1234") -> Detection:
    det = Detection(
        camera_id=camera_id,
        observed_at_utc=at,
        plate_raw=plate,
        plate_normalised=plate,
        corrections=[],
        confidence=0.9,
        pts_ms=1000.0,
        clock_confidence=1.0,
        vehicle_bbox=bbox,
    )
    session.add(det)
    session.flush()
    return det


def test_a_vehicle_centred_inside_the_zone_alerts(fixture_camera):
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    # Box centred at (1000, 550) -- inside the square.
    det = _detection(
        s, cam, {"x1": 950, "y1": 500, "x2": 1050, "y2": 600}, at=datetime.now(timezone.utc)
    )
    results = zones.evaluate_zones(s, det)
    assert len(results) == 1
    alert, action = results[0]
    assert action == "created"
    assert alert.match_type == "zone_intrusion"
    assert alert.sightings[0]["zone"] == "gate"


def test_a_vehicle_outside_the_zone_does_not_alert(fixture_camera):
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    # Centred at (200, 200) -- well clear of the square.
    det = _detection(
        s, cam, {"x1": 150, "y1": 150, "x2": 250, "y2": 250}, at=datetime.now(timezone.utc)
    )
    assert zones.evaluate_zones(s, det) == []


def test_a_box_clipping_the_corner_does_not_alert(fixture_camera):
    """Overlap is not intrusion. The centre has to be inside.

    A vehicle whose box grazes the zone boundary is passing it, and alerting on that is
    how a desk fills with events an operator learns to dismiss.
    """
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    # Box spans (700,300)-(850,450): overlaps the zone corner, centre at (775, 375).
    det = _detection(
        s, cam, {"x1": 700, "y1": 300, "x2": 850, "y2": 450}, at=datetime.now(timezone.utc)
    )
    assert zones.evaluate_zones(s, det) == []


def test_a_lingering_vehicle_is_one_alert_with_a_count(fixture_camera):
    """The cooldown: repeated detections fold into the open alert."""
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    now = datetime.now(timezone.utc)
    box = {"x1": 950, "y1": 500, "x2": 1050, "y2": 600}

    first, _ = zones.evaluate_zones(s, _detection(s, cam, box, at=now))[0]
    s.flush()
    second, action = zones.evaluate_zones(
        s, _detection(s, cam, box, at=now + timedelta(seconds=30))
    )[0]

    assert action == "deduplicated"
    assert second.id == first.id
    assert second.observation_count == 2
    assert len(second.sightings) == 2


def test_a_return_after_the_cooldown_is_a_new_alert(fixture_camera):
    """Past the window it is a fresh event, not a continuation of the old one."""
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    now = datetime.now(timezone.utc)
    box = {"x1": 950, "y1": 500, "x2": 1050, "y2": 600}

    first, _ = zones.evaluate_zones(s, _detection(s, cam, box, at=now))[0]
    s.flush()
    later = now + zones.ZONE_COOLDOWN + timedelta(minutes=1)
    second, action = zones.evaluate_zones(s, _detection(s, cam, box, at=later))[0]

    assert action == "created"
    assert second.id != first.id


def test_a_detection_with_no_vehicle_box_is_skipped(fixture_camera):
    """A plate read without a localised vehicle cannot be placed, so it is not placed."""
    s, cam = fixture_camera["session"], fixture_camera["camera_id"]
    det = _detection(s, cam, None, at=datetime.now(timezone.utc))
    assert zones.evaluate_zones(s, det) == []


def test_an_inactive_zone_never_fires(fixture_camera):
    s, cam, zid = (
        fixture_camera["session"],
        fixture_camera["camera_id"],
        fixture_camera["zone_id"],
    )
    s.execute(text("UPDATE camera_zone SET active = false WHERE id = :z"), {"z": zid})
    det = _detection(
        s, cam, {"x1": 950, "y1": 500, "x2": 1050, "y2": 600}, at=datetime.now(timezone.utc)
    )
    assert zones.evaluate_zones(s, det) == []
