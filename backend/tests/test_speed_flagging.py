"""Speed flagging: a plausible leg, an implausible one, and the REPLAY exclusion.

The exclusion test is the one that matters most. Everything else here checks arithmetic;
that one checks that the feature cannot quietly start making a claim it has no basis for.
It is written so that removing the `NOT LIKE 'REPLAY%'` clause fails the suite.

Real PostGIS, because the distance between two cameras is `ST_Distance` on geography and
a Python haversine would be testing a different function from the one that ships.
"""

from __future__ import annotations

import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics import speed
from services.registry.models import Detection

CEILINGS = {"max_speed_highway_kmph": 140.0, "max_speed_urban_kmph": 90.0}


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
    not _reachable(_URL), reason="Postgres not reachable; speed tests need real PostGIS"
)

# Two points ~9.4 km apart in Ahmedabad. Far enough to be the highway ceiling.
A_LAT, A_LON = 23.0225, 72.5714
B_LAT, B_LON = 23.1000, 72.6100

PLATE = "GJ01AB1234"


@pytest.fixture
def estate():
    """Three cameras: two real and placed, one REPLAY at the same place as the second."""
    engine = create_engine(_URL, future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    real_a, real_b, replay = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Speed Test')"),
        {"d": dept, "c": f"SPD_{suffix}"},
    )
    for cam_id, ref, lat, lon in (
        (real_a, f"spd-a-{suffix}", A_LAT, A_LON),
        (real_b, f"spd-b-{suffix}", B_LAT, B_LON),
        (replay, f"REPLAY-{suffix}", A_LAT, A_LON),
    ):
        session.execute(
            text(
                "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
                " geom, geom_source, confidence_radius_m, status, source_type,"
                " archive_mode, ownership_class, created_at, updated_at)"
                " VALUES (:id, :ref, :ref, '', :dept,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,"
                " 'survey', 10, 'ACTIVE', 'file', 'departmental', 'GOVERNMENT',"
                " now(), now())"
            ),
            {"id": cam_id, "ref": ref, "dept": dept, "lat": lat, "lon": lon},
        )
    session.commit()

    yield {
        "session": session,
        "real_a": real_a,
        "real_b": real_b,
        "replay": replay,
        "cams": (real_a, real_b, replay),
    }

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    for cam_id in (real_a, real_b, replay):
        session.execute(text("DELETE FROM alert WHERE camera_id = :c"), {"c": cam_id})
        session.execute(text("DELETE FROM detection WHERE camera_id = :c"), {"c": cam_id})
        session.execute(text("DELETE FROM camera WHERE id = :c"), {"c": cam_id})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()


def _detection(session: Session, camera_id, at: datetime, plate: str = PLATE) -> Detection:
    det = Detection(
        camera_id=camera_id,
        observed_at_utc=at,
        plate_raw=plate,
        plate_normalised=plate,
        corrections=[],
        confidence=0.9,
        pts_ms=1000.0,
        clock_confidence=1.0,
    )
    session.add(det)
    session.flush()
    return det


def test_a_plausible_leg_is_not_flagged(estate):
    """9.4 km in 15 minutes is about 38 km/h. Ordinary driving."""
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=15))
    later = _detection(s, estate["real_b"], now)
    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_an_implausible_leg_is_flagged(estate):
    """The same 9.4 km in three minutes is about 188 km/h. Past the highway ceiling."""
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=3))
    later = _detection(s, estate["real_b"], now)

    finding = speed.evaluate_speed(s, later, **CEILINGS)
    assert finding is not None
    assert finding.tolerant_kmph > finding.ceiling_kmph
    # The conservative reading is the one the flag rests on, and it must be the lower.
    assert finding.tolerant_kmph <= finding.nominal_kmph
    assert finding.ceiling_kmph == CEILINGS["max_speed_highway_kmph"]


def test_a_replay_camera_is_never_used_as_the_previous_sighting(estate):
    """The exclusion, stated as a test.

    The REPLAY camera sits at exactly camera A's position, so the pair would imply the
    same impossible speed as the real pair. It must still produce nothing: attribution
    to a harness camera is simulated, and a speed derived from it would be a fabricated
    claim rather than a measured one.

    Deleting the `NOT LIKE 'REPLAY%'` clause makes this test fail, which is the point.
    """
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["replay"], now - timedelta(minutes=3))
    later = _detection(s, estate["real_b"], now)

    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_a_replay_camera_is_never_the_flagged_end_either(estate):
    """Both ends are excluded, not just the earlier one."""
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=3))
    later = _detection(s, estate["replay"], now)

    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_two_sightings_too_close_in_time_are_not_flagged(estate):
    """Below the separation floor, clock error dominates and no claim is supportable."""
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(seconds=5))
    later = _detection(s, estate["real_b"], now)
    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_an_unplaced_camera_cannot_support_a_speed(estate):
    """No coordinate, no distance, no claim."""
    s = estate["session"]
    # geom_source must move to 'unset' with it: the registry has a check constraint
    # forbidding a null position that still claims to have been surveyed, which is the
    # schema refusing to let a camera lie about how well it is placed.
    s.execute(
        text("UPDATE camera SET geom = NULL, geom_source = 'unset' WHERE id = :c"),
        {"c": estate["real_a"]},
    )
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=3))
    later = _detection(s, estate["real_b"], now)
    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_coordinate_uncertainty_can_withdraw_a_flag(estate):
    """A speeding claim must survive the error bars on both cameras' positions.

    With a 5 km radius on each end, most of the 9.4 km "travelled" is uncertainty, and
    the conservative speed drops under the ceiling. The vehicle *might* have been
    speeding, and might-have-been is not what an alert says.
    """
    s = estate["session"]
    s.execute(
        text("UPDATE camera SET confidence_radius_m = 5000 WHERE id IN (:a, :b)"),
        {"a": estate["real_a"], "b": estate["real_b"]},
    )
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=3))
    later = _detection(s, estate["real_b"], now)

    assert speed.evaluate_speed(s, later, **CEILINGS) is None


def test_a_flag_becomes_an_alert_on_the_ordinary_alert_path(estate):
    s = estate["session"]
    now = datetime.now(timezone.utc)
    _detection(s, estate["real_a"], now - timedelta(minutes=3))
    later = _detection(s, estate["real_b"], now)

    finding = speed.evaluate_speed(s, later, **CEILINGS)
    assert finding is not None
    alert, action = speed.raise_speed_alert(s, later, finding)
    assert action == "created"
    assert alert.match_type == "speed_violation"
    assert alert.sightings[0]["speed_after_uncertainty_kmph"] == finding.tolerant_kmph
