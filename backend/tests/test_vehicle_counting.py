"""Vehicle counting: the aggregation, and the scoping that must survive it.

An aggregate is an easy place to leak. A count never returns a row naming a camera,
so a query that forgot to scope looks correct in every response body while telling an
operator how busy another department's junction was. The scoping tests here matter
more than the arithmetic ones.

The counting itself is exercised against Postgres rather than mocked: `date_trunc`,
`count(distinct ...)` and the hypertable partitioning are the things being relied on,
and none of them exist in a fake.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from services.analytics.vehicle_counting import BUCKETS, count_by_camera, count_windows


def test_buckets_are_the_three_documented_resolutions() -> None:
    assert sorted(BUCKETS) == ["day", "hour", "minute"]


def test_an_unknown_bucket_is_refused_rather_than_silently_defaulted() -> None:
    """Defaulting a typo to 'hour' would produce a plausible, wrong answer."""

    class _Unused:
        pass

    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="bucket must be one of"):
        count_windows(
            _Unused(),  # type: ignore[arg-type]  -- rejected before the session is touched
            since=now - timedelta(hours=1),
            until=now,
            bucket="hourly",
        )


# ------------------------------------------------------------------- database


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
    reason="Postgres not reachable; counting tests need the compose database",
)


@pytest.fixture
def counted():
    """One camera with a known, deliberately uneven set of detections.

    Three reads of one plate and one of another, inside a single hour, plus one read
    in the previous hour. That shape is what separates `reads` from `distinct_plates`
    and proves the bucketing is not just counting everything once.
    """
    from sqlalchemy.orm import Session, sessionmaker

    engine = create_engine(_URL, future=True)
    factory = sessionmaker(bind=engine, future=True)
    session: Session = factory()

    dept = uuid.uuid4()
    cam = uuid.uuid4()
    suffix = uuid.uuid4().hex[:8]
    # Anchored to a whole hour so the bucket boundary is unambiguous.
    base = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:id, :code, 'Counting Dept')"),
        {"id": dept, "code": f"CNT_{suffix}"},
    )
    session.execute(
        text(
            "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
            " geom_source, status, source_type, archive_mode, ownership_class,"
            " created_at, updated_at)"
            " VALUES (:id, :ref, :ref, '', :dept, 'unset', 'ACTIVE', 'file',"
            " 'departmental', 'GOVERNMENT', now(), now())"
        ),
        {"id": cam, "ref": f"count-{suffix}", "dept": dept},
    )

    rows = [
        (base, f"GJ01AA{suffix[:4].upper()}"),
        (base + timedelta(seconds=10), f"GJ01AA{suffix[:4].upper()}"),
        (base + timedelta(seconds=20), f"GJ01AA{suffix[:4].upper()}"),
        (base + timedelta(seconds=30), f"GJ02BB{suffix[:4].upper()}"),
        (base - timedelta(hours=1), f"GJ03CC{suffix[:4].upper()}"),
    ]
    for observed, plate in rows:
        session.execute(
            text(
                "INSERT INTO detection (id, observed_at_utc, camera_id, plate_raw,"
                " plate_normalised, corrections, confidence, pts_ms, ingested_at_utc,"
                " clock_confidence)"
                " VALUES (:id, :obs, :cam, :plate, :plate, '[]'::jsonb, 0.9, 0, now(), 1.0)"
            ),
            {"id": uuid.uuid4(), "obs": observed, "cam": cam, "plate": plate},
        )
    session.commit()

    yield session, str(cam), base

    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    session.execute(text("DELETE FROM detection WHERE camera_id = :c"), {"c": cam})
    session.execute(text("DELETE FROM camera WHERE id = :c"), {"c": cam})
    session.execute(text("DELETE FROM department WHERE id = :d"), {"d": dept})
    session.commit()
    session.close()
    engine.dispose()


def test_reads_and_distinct_plates_are_different_numbers(counted) -> None:
    """The distinction the whole module exists to preserve."""
    session, cam, base = counted
    windows = count_windows(
        session,
        since=base - timedelta(minutes=5),
        until=base + timedelta(minutes=5),
        bucket="hour",
        camera_ids=[cam],
    )
    assert len(windows) == 1
    (w,) = windows
    assert w.reads == 4, "four rows were inserted inside this hour"
    assert w.distinct_plates == 2, "but they are only two registrations"
    assert w.cameras_reporting == 1


def test_buckets_split_on_the_observation_hour(counted) -> None:
    session, cam, base = counted
    windows = count_windows(
        session,
        since=base - timedelta(hours=2),
        until=base + timedelta(minutes=5),
        bucket="hour",
        camera_ids=[cam],
    )
    assert len(windows) == 2, "one read sits in the previous hour"
    assert [w.reads for w in windows] == [1, 4]
    # Ordered oldest first, so a chart does not have to re-sort.
    assert windows[0].bucket_start_utc < windows[1].bucket_start_utc


def test_a_period_with_no_detections_yields_no_buckets(counted) -> None:
    """Absence is reported as absence, not as a fabricated zero."""
    session, cam, base = counted
    windows = count_windows(
        session,
        since=base + timedelta(days=30),
        until=base + timedelta(days=31),
        bucket="hour",
        camera_ids=[cam],
    )
    assert windows == []


def test_per_camera_totals_name_the_camera(counted) -> None:
    session, cam, base = counted
    rows = count_by_camera(
        session,
        since=base - timedelta(hours=2),
        until=base + timedelta(minutes=5),
        camera_ids=[cam],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.camera_id == cam
    assert row.camera_ref.startswith("count-")
    assert row.reads == 5
    assert row.distinct_plates == 3
    assert row.first_seen_utc is not None and row.last_seen_utc is not None
    assert row.first_seen_utc < row.last_seen_utc


def test_scoping_to_another_camera_returns_nothing(counted) -> None:
    """The leak this module could plausibly have: counting rows you may not see."""
    session, _cam, base = counted
    other = str(uuid.uuid4())
    windows = count_windows(
        session,
        since=base - timedelta(hours=2),
        until=base + timedelta(minutes=5),
        bucket="hour",
        camera_ids=[other],
    )
    assert windows == []
    assert (
        count_by_camera(
            session,
            since=base - timedelta(hours=2),
            until=base + timedelta(minutes=5),
            camera_ids=[other],
        )
        == []
    )
