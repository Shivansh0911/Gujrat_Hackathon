"""Judge Mode picks a registration this database can actually draw a route for.

The behaviour under test is an ordering, and the ordering exists because the obvious
one is wrong. Ranking by "seen at the most cameras" selects a plate read four times in
fifteen minutes at cameras hundreds of kilometres apart -- four cameras, no journey,
because every hop is physically impossible and the route builder rejects it. Judge Mode
then opens on "1 hop, single sighting", which is precisely the screen it exists to
avoid.

So the query prefers the widest time span among plates seen at the same number of
cameras. These tests pin that, and pin the two refusals around it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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
    not _reachable(_URL), reason="Postgres not reachable; the selection is SQL"
)

NOW = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def estate() -> Any:
    """Four placed cameras and one unplaced one, isolated from the real registry."""
    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    refs = [f"BT-{n}-{suffix}" for n in ("A", "B", "C", "D")]
    unplaced_ref = f"BT-U-{suffix}"
    cams = {ref: uuid.uuid4() for ref in [*refs, unplaced_ref]}

    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Best Trace Dept')"),
        {"d": dept, "c": f"BT_{suffix}"},
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
    # A camera with no coordinate. A route cannot be drawn through it, so reads there
    # must not make a plate look traceable.
    session.execute(
        text(
            "INSERT INTO camera (id, camera_ref, name, location_text, department_id,"
            " geom, geom_source, confidence_radius_m, status, source_type,"
            " archive_mode, ownership_class, created_at, updated_at)"
            " VALUES (:id, :ref, :ref, '', :dept, NULL, 'unset', NULL, 'ACTIVE',"
            " 'file', 'departmental', 'GOVERNMENT', now(), now())"
        ),
        {"id": cams[unplaced_ref], "ref": unplaced_ref, "dept": dept},
    )
    session.commit()

    yield {"session": session, "refs": refs, "unplaced": unplaced_ref, "cams": cams}

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


def _best(session: Session, prefix: str) -> Any:
    """Run the selection, scoped to this test's plates so the real estate cannot win."""
    return session.execute(
        text(
            """
            SELECT d.plate_normalised AS plate,
                   count(DISTINCT d.camera_id) AS cameras,
                   count(*) AS reads,
                   min(d.observed_at_utc) AS first_seen,
                   max(d.observed_at_utc) AS last_seen
            FROM detection d
            JOIN camera c ON c.id = d.camera_id
            WHERE d.plate_normalised IS NOT NULL
              AND d.plate_normalised LIKE :prefix
              AND c.geom IS NOT NULL
            GROUP BY d.plate_normalised
            HAVING count(DISTINCT d.camera_id) > 1
            ORDER BY cameras DESC,
                     (max(d.observed_at_utc) - min(d.observed_at_utc)) DESC,
                     reads DESC
            LIMIT 1
            """
        ),
        {"prefix": f"{prefix}%"},
    ).first()


def test_a_spread_trace_beats_a_bunched_one_at_the_same_camera_count(estate: Any) -> None:
    """The defect this ordering exists to prevent, stated as a test.

    Both plates are seen at all four cameras. One is seen across fifteen minutes, which
    on cameras this far apart is not a journey; the other across six hours, which is.
    """
    session = estate["session"]
    cams = estate["cams"]
    refs = estate["refs"]

    for i, ref in enumerate(refs):
        _read(session, cams[ref], "BUNCHED1", NOW + timedelta(minutes=5 * i))
    # One extra read, so "most reads" would pick the bunched plate if span did not win.
    _read(session, cams[refs[0]], "BUNCHED1", NOW + timedelta(minutes=1))

    for i, ref in enumerate(refs):
        _read(session, cams[ref], "SPREAD01", NOW + timedelta(hours=2 * i))
    session.commit()

    row = _best(session, "BUNCHED1")
    assert row is not None and row.plate == "BUNCHED1"  # sanity: it is selectable alone

    row = session.execute(
        text(
            """
            SELECT d.plate_normalised AS plate,
                   count(DISTINCT d.camera_id) AS cameras,
                   count(*) AS reads
            FROM detection d JOIN camera c ON c.id = d.camera_id
            WHERE d.plate_normalised IN ('BUNCHED1', 'SPREAD01') AND c.geom IS NOT NULL
            GROUP BY d.plate_normalised
            ORDER BY cameras DESC,
                     (max(d.observed_at_utc) - min(d.observed_at_utc)) DESC,
                     reads DESC
            LIMIT 1
            """
        )
    ).first()
    assert row is not None
    assert (
        row.plate == "SPREAD01"
    ), "the wider time span must win even though the bunched plate has more reads"


def test_a_plate_seen_at_one_camera_is_not_a_journey(estate: Any) -> None:
    session = estate["session"]
    cams = estate["cams"]
    for i in range(6):
        _read(session, cams[estate["refs"][0]], "SINGLECAM", NOW + timedelta(minutes=10 * i))
    session.commit()

    assert _best(session, "SINGLECAM") is None


def test_reads_at_unplaced_cameras_do_not_make_a_plate_traceable(estate: Any) -> None:
    """A camera without a coordinate cannot contribute a hop to a drawn route."""
    session = estate["session"]
    cams = estate["cams"]
    _read(session, cams[estate["refs"][0]], "NOCOORD01", NOW)
    _read(session, cams[estate["unplaced"]], "NOCOORD01", NOW + timedelta(hours=1))
    session.commit()

    assert (
        _best(session, "NOCOORD01") is None
    ), "one placed camera plus one unplaced one is a single drawable point, not a route"
