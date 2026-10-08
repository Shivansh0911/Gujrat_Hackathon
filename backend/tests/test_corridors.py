"""Corridor transit times, and the several ways the report declines to claim one.

The interesting behaviour here is all refusal. A median of three observations is a
number with the authority of a statistic and the reliability of an anecdote; a
"congestion" flag raised by comparing Monday rush hour against Sunday afternoon is worse
than no flag at all. Most of these tests check that the report withholds rather than
reports.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics.corridors import (
    MAX_TRANSIT,
    MIN_TRANSITS,
    corridor_report,
)


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
    not _reachable(_URL), reason="Postgres not reachable; corridor transits are SQL"
)

#: A Wednesday, so the hour-of-week baseline has an unambiguous day to match.
NOW = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
WINDOW = {"from_": NOW - timedelta(hours=1), "to": NOW + timedelta(hours=2)}


@pytest.fixture
def corridor() -> Any:
    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    refs = [f"COR-{n}-{suffix}" for n in ("A", "B", "C")]
    cams = {ref: uuid.uuid4() for ref in refs}

    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Corridor Dept')"),
        {"d": dept, "c": f"COR_{suffix}"},
    )
    for index, ref in enumerate(refs):
        session.execute(
            text(
                "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
                " geom, geom_source, confidence_radius_m, status, source_type, archive_mode,"
                " ownership_class, created_at, updated_at)"
                " VALUES (:id, :ref, :ref, '', :dept,"
                " ST_SetSRID(ST_MakePoint(:lon, 23.0225), 4326)::geography, 'approximate', 300,"
                " 'ACTIVE', 'file', 'departmental', 'GOVERNMENT', now(), now())"
            ),
            {"id": cams[ref], "ref": ref, "dept": dept, "lon": 72.5714 + index * 0.02},
        )
    session.commit()

    yield {"session": session, "refs": refs, "cams": cams, "dept": dept}

    session.rollback()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    ids = [str(v) for v in cams.values()]
    session.execute(
        text("DELETE FROM detection WHERE camera_id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
    )
    session.execute(text("DELETE FROM camera WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()
    engine.dispose()


def _read(session: Session, cam: uuid.UUID, plate: str, at: datetime) -> None:
    session.execute(
        text(
            "INSERT INTO detection (id, camera_id, observed_at_utc, plate_raw,"
            " plate_normalised, corrections, confidence, pts_ms, ingested_at_utc,"
            " clock_confidence)"
            " VALUES (gen_random_uuid(), :cam, :at, :plate, :plate, '[]'::jsonb, 0.9,"
            " 1000, now(), 1.0)"
        ),
        {"cam": cam, "at": at, "plate": plate},
    )


def _traffic(corridor: Any, count: int, *, transit_s: int = 120, start: datetime = NOW) -> None:
    """`count` vehicles crossing A then B, each taking `transit_s`."""
    session, refs, cams = corridor["session"], corridor["refs"], corridor["cams"]
    for i in range(count):
        plate = f"GJ{i:02d}XX{i:04d}"
        at = start + timedelta(seconds=i * 7)
        _read(session, cams[refs[0]], plate, at)
        _read(session, cams[refs[1]], plate, at + timedelta(seconds=transit_s))
    session.flush()


# ------------------------------------------------------------------- measurement


def test_transit_times_are_measured_between_consecutive_cameras(corridor: Any) -> None:
    _traffic(corridor, MIN_TRANSITS + 3, transit_s=120)
    report = corridor_report(
        corridor["session"], name="Test corridor", camera_refs=corridor["refs"], **WINDOW
    )
    leg = report.legs[0]
    assert leg.transits == MIN_TRANSITS + 3
    assert leg.median_s == pytest.approx(120.0, abs=1.0)
    corridor["session"].rollback()


def test_the_corridor_is_ordered_so_each_leg_is_one_direction(corridor: Any) -> None:
    """A to B and B to A are different roads at different times of day."""
    _traffic(corridor, MIN_TRANSITS + 1)
    report = corridor_report(
        corridor["session"], name="Forward", camera_refs=corridor["refs"], **WINDOW
    )
    assert [leg.from_camera_ref for leg in report.legs] == corridor["refs"][:-1]

    reverse = corridor_report(
        corridor["session"], name="Reverse", camera_refs=list(reversed(corridor["refs"])), **WINDOW
    )
    # The same vehicles went forward only, so the reverse direction sees nothing.
    assert all(leg.transits == 0 for leg in reverse.legs)
    corridor["session"].rollback()


def test_the_distance_between_each_pair_is_reported(corridor: Any) -> None:
    _traffic(corridor, MIN_TRANSITS + 1)
    report = corridor_report(
        corridor["session"], name="Test", camera_refs=corridor["refs"], **WINDOW
    )
    assert report.legs[0].distance_m is not None and report.legs[0].distance_m > 0
    corridor["session"].rollback()


# ------------------------------------------------------------------- withholding


def test_too_few_transits_withholds_the_median(corridor: Any) -> None:
    """A median of three is a number with the authority of a statistic."""
    _traffic(corridor, MIN_TRANSITS - 2)
    report = corridor_report(
        corridor["session"], name="Thin", camera_refs=corridor["refs"], **WINDOW
    )
    leg = report.legs[0]
    assert 0 < leg.transits < MIN_TRANSITS
    assert leg.median_s is None, "the count is reported; the median is not invented"
    corridor["session"].rollback()


def test_no_baseline_means_no_congestion_flag(corridor: Any) -> None:
    """This deployment has no history, so 'versus normal' cannot mean anything yet."""
    _traffic(corridor, MIN_TRANSITS + 5)
    report = corridor_report(
        corridor["session"], name="Fresh", camera_refs=corridor["refs"], **WINDOW
    )
    leg = report.legs[0]
    assert leg.baseline_median_s is None
    assert leg.ratio_to_baseline is None
    assert leg.congested is False
    assert "too little history" in report.note
    corridor["session"].rollback()


def test_a_vehicle_that_stopped_for_an_hour_is_not_a_transit(corridor: Any) -> None:
    """Beyond the bound the pair describes two journeys, not one slow leg."""
    session, refs, cams = corridor["session"], corridor["refs"], corridor["cams"]
    _read(session, cams[refs[0]], "GJ99SLOW99", NOW)
    _read(
        session,
        cams[refs[1]],
        "GJ99SLOW99",
        NOW + MAX_TRANSIT + timedelta(minutes=5),
    )
    session.flush()
    report = corridor_report(
        session,
        name="Stopped",
        camera_refs=corridor["refs"],
        from_=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=3),
    )
    assert report.legs[0].transits == 0
    session.rollback()


def test_two_reads_in_the_same_instant_are_not_a_transit(corridor: Any) -> None:
    session, refs, cams = corridor["session"], corridor["refs"], corridor["cams"]
    _read(session, cams[refs[0]], "GJ88FAST88", NOW)
    _read(session, cams[refs[1]], "GJ88FAST88", NOW + timedelta(seconds=1))
    session.flush()
    report = corridor_report(session, name="Instant", camera_refs=corridor["refs"], **WINDOW)
    assert report.legs[0].transits == 0
    session.rollback()


# --------------------------------------------------------------------- honesty


def test_the_report_says_why_a_low_count_may_not_mean_light_traffic(corridor: Any) -> None:
    """On an estate that cannot read plates, few transits means few legible plates."""
    report = corridor_report(
        corridor["session"], name="Empty", camera_refs=corridor["refs"], **WINDOW
    )
    assert report.total_transits == 0
    assert "legible plate" in report.note
    corridor["session"].rollback()


def test_a_harness_camera_in_the_corridor_is_called_out(corridor: Any) -> None:
    """REPLAY positions replay one recording; a transit through them describes the harness."""
    session = corridor["session"]
    replay_ref = session.execute(
        text("SELECT camera_ref FROM camera WHERE camera_ref LIKE 'REPLAY%' LIMIT 1")
    ).scalar()
    if replay_ref is None:
        pytest.skip("no REPLAY camera in this database")
    report = corridor_report(
        session, name="Mixed", camera_refs=[corridor["refs"][0], replay_ref], **WINDOW
    )
    assert "harness positions" in report.note
    session.rollback()


def test_an_unknown_camera_is_refused_rather_than_skipped(corridor: Any) -> None:
    """Silently dropping a camera would shorten the corridor without saying so."""
    with pytest.raises(ValueError, match="unknown cameras"):
        corridor_report(
            corridor["session"],
            name="Bad",
            camera_refs=[corridor["refs"][0], "NO-SUCH-CAMERA"],
            **WINDOW,
        )
    corridor["session"].rollback()
