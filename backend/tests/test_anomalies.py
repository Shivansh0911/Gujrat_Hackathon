"""Cloned plates, convoys and dwell, against controlled rows in a real database.

These run against Postgres rather than a fake, because all three findings are SQL: the
distance is PostGIS, the windowing is an interval join, and a test that mocked those
would be testing the mock. The fixtures build exactly the rows each case needs and
delete them again.

The property under test throughout is restraint. Each detector is easy to make fire;
the work is in making it refuse. A cloned-plate finding opens an investigation against a
registration, a convoy finding says two named vehicles are travelling together, and both
are claims about people.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics.anomalies import (
    CONVOY_MIN_CAMERAS,
    DWELL_MAX_DRIFT_RATIO,
    IMPOSSIBLE_KMPH,
    find_cloned_plates,
    find_convoys,
    find_dwells,
)
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
    not _reachable(_URL), reason="Postgres not reachable; anomaly queries are SQL"
)

NOW = datetime(2026, 10, 9, 9, 0, 0, tzinfo=timezone.utc)
_VEC = "[" + ",".join(["0.1"] * APPEARANCE_DIM) + "]"


@pytest.fixture
def estate() -> Any:
    """Three placed cameras far enough apart to make impossible speeds possible.

    Ahmedabad to Surat is ~230 km; the fixture uses coordinates roughly that far apart
    so that "seen at both within two minutes" is unambiguously impossible rather than
    marginal.
    """
    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    cams = {name: uuid.uuid4() for name in ("A", "B", "C")}
    coords = {"A": (72.5714, 23.0225), "B": (72.8311, 21.1702), "C": (72.6000, 23.1000)}

    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Anomaly Dept')"),
        {"d": dept, "c": f"ANOM_{suffix}"},
    )
    for name, cam_id in cams.items():
        lon, lat = coords[name]
        session.execute(
            text(
                "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
                " geom, geom_source, confidence_radius_m, status, source_type, archive_mode,"
                " ownership_class, created_at, updated_at)"
                " VALUES (:id, :ref, :ref, '', :dept,"
                " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, 'approximate', 300,"
                " 'ACTIVE', 'file', 'departmental', 'GOVERNMENT', now(), now())"
            ),
            {"id": cam_id, "ref": f"ANOM-{name}-{suffix}", "dept": dept, "lon": lon, "lat": lat},
        )
    session.commit()

    yield {"session": session, "cams": cams, "suffix": suffix, "dept": dept}

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    ids = tuple(str(v) for v in cams.values())
    session.execute(
        text("DELETE FROM vehicle_sighting WHERE camera_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": list(ids)},
    )
    session.execute(
        text("DELETE FROM detection WHERE camera_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": list(ids)},
    )
    session.execute(
        text("DELETE FROM camera WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": list(ids)}
    )
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()
    engine.dispose()


def _detect(
    session: Session, cam: uuid.UUID, plate: str, at: datetime, pts: float = 1000.0
) -> None:
    session.execute(
        text(
            "INSERT INTO detection (id, camera_id, observed_at_utc, plate_raw,"
            " plate_normalised, corrections, confidence, pts_ms, ingested_at_utc,"
            " clock_confidence, crop_path)"
            " VALUES (gen_random_uuid(), :cam, :at, :plate, :plate, '[]'::jsonb, 0.9,"
            " :pts, now(), 1.0, :crop)"
        ),
        {"cam": cam, "at": at, "plate": plate, "pts": pts, "crop": f"{plate}.jpg"},
    )


def _sight(
    session: Session,
    cam: uuid.UUID,
    *,
    at: datetime,
    pts: float,
    last_pts: float,
    displacement: float,
    box_w: float,
    frames: int = 10,
) -> None:
    session.execute(
        text(
            "INSERT INTO vehicle_sighting (id, camera_id, observed_at_utc, vehicle_bbox,"
            " vehicle_class, vehicle_class_confidence, track_id, appearance_vector,"
            " appearance_kind, aspect_ratio, rel_area, rel_cx, rel_cy, quality_score,"
            " pts_ms, last_pts_ms, frames_seen, track_displacement_px, clock_confidence,"
            " ingested_at_utc)"
            " VALUES (gen_random_uuid(), :cam, :at, CAST(:box AS jsonb), 'car', 0.9,"
            " :track, CAST(:vec AS vector), :kind, 1.4, 0.05, 0.5, 0.5, 0.8,"
            " :pts, :last_pts, :frames, :disp, 1.0, now())"
        ),
        {
            "cam": cam,
            "at": at,
            "box": '{"x1": 100, "y1": 100, "x2": %d, "y2": 300}' % int(100 + box_w),
            "track": uuid.uuid4().hex[:16],
            "vec": _VEC,
            "kind": APPEARANCE_KIND,
            "pts": pts,
            "last_pts": last_pts,
            "frames": frames,
            "disp": displacement,
        },
    )


WINDOW = {"from_": NOW - timedelta(hours=6), "to": NOW + timedelta(hours=6)}


# ------------------------------------------------------------------ cloned plates


def test_a_plate_in_two_places_at_once_is_flagged(estate: Any) -> None:
    """~230 km apart, two minutes apart. No road vehicle explains it."""
    s = estate["session"]
    _detect(s, estate["cams"]["A"], "GJ01AB1234", NOW)
    _detect(s, estate["cams"]["B"], "GJ01AB1234", NOW + timedelta(minutes=2))
    s.flush()

    found = find_cloned_plates(s, **WINDOW)
    assert len(found) == 1
    assert found[0].plate == "GJ01AB1234"
    assert found[0].implied_kmph > IMPOSSIBLE_KMPH
    s.rollback()


def test_both_hypotheses_are_returned_not_one(estate: Any) -> None:
    """The platform cannot tell a clone from a misread, and must not pretend to.

    Only a human comparing the two crops can say which it is, so both crops and both
    readings travel with the finding.
    """
    s = estate["session"]
    _detect(s, estate["cams"]["A"], "GJ02CD5678", NOW)
    _detect(s, estate["cams"]["B"], "GJ02CD5678", NOW + timedelta(minutes=2))
    s.flush()

    finding = find_cloned_plates(s, **WINDOW)[0]
    assert len(finding.hypotheses) == 2
    assert any("two different vehicles" in h for h in finding.hypotheses)
    assert any("one of the two reads is wrong" in h for h in finding.hypotheses)
    assert finding.from_crop_path and finding.to_crop_path
    s.rollback()


def test_ordinary_speeding_is_not_called_a_cloned_plate(estate: Any) -> None:
    """95 km/h in a 60 zone is a traffic offence, not a vehicle in two places.

    Conflating them files a cloned plate as a speeding ticket, which is the defect this
    detector exists to fix -- in the other direction.
    """
    s = estate["session"]
    # A and C are ~8 km apart; six minutes between them is about 100 km/h.
    _detect(s, estate["cams"]["A"], "GJ03EF9012", NOW)
    _detect(s, estate["cams"]["C"], "GJ03EF9012", NOW + timedelta(minutes=6))
    s.flush()

    assert find_cloned_plates(s, **WINDOW) == []
    s.rollback()


def test_two_sightings_seconds_apart_are_refused(estate: Any) -> None:
    """Below the separation floor the timestamps cannot support any claim."""
    s = estate["session"]
    _detect(s, estate["cams"]["A"], "GJ04GH3456", NOW)
    _detect(s, estate["cams"]["B"], "GJ04GH3456", NOW + timedelta(seconds=3))
    s.flush()
    assert find_cloned_plates(s, **WINDOW) == []
    s.rollback()


def test_coordinate_uncertainty_is_handed_to_the_vehicle(estate: Any) -> None:
    """A pair is flagged only when it is impossible even after the error bars.

    Both fixture cameras carry a 300 m radius, so 600 m is subtracted before the speed
    is computed. At 230 km it changes nothing, which is the point: the allowance must
    not be able to rescue a genuinely impossible hop.
    """
    s = estate["session"]
    _detect(s, estate["cams"]["A"], "GJ05IJ7890", NOW)
    _detect(s, estate["cams"]["B"], "GJ05IJ7890", NOW + timedelta(minutes=2))
    s.flush()
    finding = find_cloned_plates(s, **WINDOW)[0]
    assert finding.tolerance_m == pytest.approx(600.0)
    s.rollback()


# ------------------------------------------------------------------------ convoys


def test_two_plates_seen_together_at_three_cameras_is_a_convoy(estate: Any) -> None:
    s = estate["session"]
    for index, cam in enumerate(("A", "B", "C")):
        at = NOW + timedelta(minutes=index * 30)
        _detect(s, estate["cams"][cam], "GJ10AA1111", at)
        _detect(s, estate["cams"][cam], "GJ10BB2222", at + timedelta(seconds=5))
    s.flush()

    convoys = find_convoys(s, **WINDOW)
    assert len(convoys) == 1
    assert {convoys[0].plate_a, convoys[0].plate_b} == {"GJ10AA1111", "GJ10BB2222"}
    assert convoys[0].cameras == 3
    s.rollback()


def test_two_cameras_is_a_shared_route_not_a_convoy(estate: Any) -> None:
    """Most traffic on a corridor shares two cameras. Three is the lowest honest bar."""
    s = estate["session"]
    for index, cam in enumerate(("A", "B")):
        at = NOW + timedelta(minutes=index * 30)
        _detect(s, estate["cams"][cam], "GJ11AA1111", at)
        _detect(s, estate["cams"][cam], "GJ11BB2222", at + timedelta(seconds=5))
    s.flush()
    assert find_convoys(s, **WINDOW) == []
    s.rollback()


def test_vehicles_far_apart_in_time_are_not_travelling_together(estate: Any) -> None:
    s = estate["session"]
    for index, cam in enumerate(("A", "B", "C")):
        at = NOW + timedelta(minutes=index * 30)
        _detect(s, estate["cams"][cam], "GJ12AA1111", at)
        # Ten minutes later at each camera: same route, not the same journey.
        _detect(s, estate["cams"][cam], "GJ12BB2222", at + timedelta(minutes=10))
    s.flush()
    assert find_convoys(s, **WINDOW) == []
    s.rollback()


def test_the_co_sightings_behind_a_convoy_are_returned(estate: Any) -> None:
    """A claim about two named vehicles has to be checkable."""
    s = estate["session"]
    for index, cam in enumerate(("A", "B", "C")):
        at = NOW + timedelta(minutes=index * 30)
        _detect(s, estate["cams"][cam], "GJ13AA1111", at)
        _detect(s, estate["cams"][cam], "GJ13BB2222", at + timedelta(seconds=5))
    s.flush()
    convoy = find_convoys(s, **WINDOW)[0]
    assert len(convoy.co_sightings) >= CONVOY_MIN_CAMERAS
    assert all("camera_ref" in c for c in convoy.co_sightings)
    s.rollback()


# -------------------------------------------------------------------------- dwell


def test_a_long_stationary_pass_is_a_dwell(estate: Any) -> None:
    s = estate["session"]
    _sight(
        s,
        estate["cams"]["A"],
        at=NOW,
        pts=1000.0,
        last_pts=1000.0 + 45_000.0,
        displacement=20.0,
        box_w=400.0,
    )
    s.flush()
    found = find_dwells(s, **WINDOW)
    assert len(found) == 1
    assert found[0].dwell_seconds == pytest.approx(45.0)
    s.rollback()


def test_a_long_pass_that_crossed_the_frame_is_not_a_dwell(estate: Any) -> None:
    """The longest pass in the real footage is not a dwell, and that is correct.

    A vehicle in view for a long time because it is moving slowly is not abandoned, and
    a detector that cannot tell those apart would flag every traffic queue.
    """
    s = estate["session"]
    _sight(
        s,
        estate["cams"]["A"],
        at=NOW,
        pts=1000.0,
        last_pts=1000.0 + 45_000.0,
        displacement=380.0,  # 0.95 of the box width
        box_w=400.0,
    )
    s.flush()
    assert find_dwells(s, **WINDOW) == []
    s.rollback()


def test_a_brief_stationary_pass_is_not_a_dwell(estate: Any) -> None:
    s = estate["session"]
    _sight(
        s,
        estate["cams"]["A"],
        at=NOW,
        pts=1000.0,
        last_pts=3_000.0,
        displacement=5.0,
        box_w=400.0,
    )
    s.flush()
    assert find_dwells(s, **WINDOW) == []
    s.rollback()


def test_drift_is_judged_against_the_box_not_in_absolute_pixels(estate: Any) -> None:
    """The same movement means different things near and far, and no calibration exists.

    Two passes drift 150 px. For the vehicle filling the frame that is a twitch; for the
    one at the far end it is most of its own length.
    """
    s = estate["session"]
    _sight(
        s, estate["cams"]["A"], at=NOW, pts=0.0, last_pts=45_000.0, displacement=150.0, box_w=1000.0
    )
    _sight(
        s, estate["cams"]["B"], at=NOW, pts=0.0, last_pts=45_000.0, displacement=150.0, box_w=200.0
    )
    s.flush()

    found = find_dwells(s, **WINDOW)
    assert len(found) == 1, "only the near vehicle's drift is small relative to itself"
    assert found[0].box_width_px == pytest.approx(1000.0)
    assert 150.0 / 1000.0 <= DWELL_MAX_DRIFT_RATIO < 150.0 / 200.0
    s.rollback()


def test_a_dwell_needs_no_plate_and_no_cross_camera_identity(estate: Any) -> None:
    """This is why dwell is the most reliable of the three findings.

    The sighting written here has no linked plate read at all, which is the state most
    sightings on the government estate will be in.
    """
    s = estate["session"]
    _sight(
        s, estate["cams"]["A"], at=NOW, pts=0.0, last_pts=60_000.0, displacement=10.0, box_w=500.0
    )
    s.flush()
    found = find_dwells(s, **WINDOW)
    assert len(found) == 1
    assert found[0].vehicle_class == "car"
    s.rollback()
