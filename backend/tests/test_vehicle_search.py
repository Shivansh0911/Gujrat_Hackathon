"""Searching for a vehicle that has no registration.

The behaviour worth pinning is the ceiling. `candidate_journey` may return Probable,
because a confirmed plate read anchors the chain and the rest of it inherits that
credibility. Nothing anchors a search started from a picture, so every result here is
a Candidate however good the score is -- two vehicles that look alike are two vehicles
that look alike.

The other thing worth pinning is the difference between "nothing matched" and "this
could never have matched", which is the distinction `plate_linking` got wrong by
reporting zero links without having examined a row.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.analytics.model_ids import APPEARANCE_DIM, APPEARANCE_KIND
from services.analytics.vehicle_search import (
    AnchorUnavailable,
    browse_sightings,
    find_similar,
)
from services.registry.enums import EvidenceTier


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
    not _reachable(_URL), reason="Postgres not reachable; the search is a vector query"
)

NOW = datetime(2026, 10, 7, 9, 0, 0, tzinfo=timezone.utc)
SPEEDS = {"max_speed_urban_kmph": 80.0, "max_speed_highway_kmph": 120.0}


def _vector(seed: float) -> str:
    """A descriptor of the right width. Values are deliberately close, so two of these
    sit well inside the 0.05 gate and the test exercises the tier rule rather than the
    distance threshold."""
    return "[" + ",".join(f"{seed + i * 1e-6:.8f}" for i in range(APPEARANCE_DIM)) + "]"


@pytest.fixture
def estate() -> Any:
    engine = create_engine(_URL or "", future=True)
    session: Session = sessionmaker(bind=engine, future=True)()
    session.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))

    suffix = uuid.uuid4().hex[:8]
    dept = uuid.uuid4()
    refs = [f"VS-{n}-{suffix}" for n in ("A", "B")]
    cams = {ref: uuid.uuid4() for ref in refs}

    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Search Dept')"),
        {"d": dept, "c": f"VS_{suffix}"},
    )
    # Two kilometres apart, so a pass twenty minutes later is comfortably plausible.
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
    session: Session,
    cam: uuid.UUID,
    at: datetime,
    *,
    vector: str | None,
    cls: str = "car",
    colour: str = '{"L": 60.0, "a": 2.0, "b": -1.0}',
) -> uuid.UUID:
    sid = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO vehicle_sighting (id, camera_id, observed_at_utc, vehicle_class,"
            " vehicle_class_confidence, vehicle_bbox, rel_area, rel_cx, rel_cy,"
            " aspect_ratio, quality_score, frames_seen, track_id, pts_ms, last_pts_ms,"
            " clock_confidence, ingested_at_utc, track_displacement_px,"
            " appearance_vector, appearance_kind, dominant_colour_lab)"
            " VALUES (:id, :cam, :at, :cls, 0.9, CAST(:bbox AS jsonb), 0.08, 0.5, 0.5,"
            " 1.25, 0.9, 6, :track, 1000, 2000, 1.0, now(), 40,"
            " CAST(:vec AS vector), :kind, CAST(:colour AS jsonb))"
        ),
        {
            "id": sid,
            "cam": cam,
            "at": at,
            "cls": cls,
            "bbox": '{"x1":0,"y1":0,"x2":120,"y2":90}',
            "track": uuid.uuid4().hex[:12],
            "vec": vector,
            "kind": APPEARANCE_KIND if vector else None,
            "colour": colour,
        },
    )
    return sid


def test_a_vehicle_with_no_plate_can_still_be_searched_for(estate: Any) -> None:
    """The capability, stated as a test: no registration is involved anywhere."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    anchor = _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10))
    _sighting(session, cams[refs[1]], NOW + timedelta(minutes=20), vector=_vector(0.10))
    session.commit()

    result = find_similar(
        session,
        sighting_id=anchor,
        camera_ids=list(cams.values()),
        from_=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=2),
        **SPEEDS,
    )
    assert result.anchor.plate_read is None
    assert len(result.matches) == 1
    assert result.matches[0].camera_ref == refs[1]

    reads = session.execute(
        text("SELECT count(*) FROM detection WHERE camera_id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": [str(c) for c in cams.values()]},
    ).scalar()
    assert reads == 0, "no plate was read anywhere; that is the whole point"


def test_every_match_is_a_candidate_however_good_the_score(estate: Any) -> None:
    """The ceiling. An identical descriptor, matching class and matching colour would
    earn Probable on the journey path, where a confirmed plate anchors the chain.
    Nothing anchors this one."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    anchor = _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10), cls="bus")
    for minutes in (18, 22, 26):
        _sighting(
            session,
            cams[refs[1]],
            NOW + timedelta(minutes=minutes),
            vector=_vector(0.10),
            cls="bus",
        )
    session.commit()

    result = find_similar(
        session,
        sighting_id=anchor,
        camera_ids=list(cams.values()),
        from_=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=2),
        **SPEEDS,
    )
    assert result.matches, "an identical descriptor on a reachable camera must match"
    assert {m.tier for m in result.matches} == {EvidenceTier.CANDIDATE}
    assert max(m.score.total for m in result.matches) > 0.8, (
        "the score must stay high -- the downgrade is applied to the claim, not by "
        "weakening the scorer"
    )


def test_a_sighting_without_a_fingerprint_says_so_rather_than_returning_nothing(
    estate: Any,
) -> None:
    """ "Nothing matched" and "this could never match" are different answers."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    anchor = _sighting(session, cams[refs[0]], NOW, vector=None)
    session.commit()

    with pytest.raises(AnchorUnavailable) as excinfo:
        find_similar(
            session,
            sighting_id=anchor,
            camera_ids=list(cams.values()),
            from_=NOW - timedelta(hours=1),
            to=NOW + timedelta(hours=2),
            **SPEEDS,
        )
    assert "fingerprint" in str(excinfo.value)


def test_a_sighting_outside_the_actors_scope_is_not_found(estate: Any) -> None:
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    anchor = _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10))
    session.commit()

    with pytest.raises(AnchorUnavailable):
        find_similar(
            session,
            sighting_id=anchor,
            camera_ids=[cams[refs[1]]],  # the anchor's camera is not visible
            from_=NOW - timedelta(hours=1),
            to=NOW + timedelta(hours=2),
            **SPEEDS,
        )


def test_an_impossible_hop_is_refused_however_identical_it_looks(estate: Any) -> None:
    """The plausibility gate runs before the score, so appearance cannot buy its way
    past physics."""
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    anchor = _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10))
    # Two kilometres away, one second later: roughly 7,000 km/h.
    _sighting(session, cams[refs[1]], NOW + timedelta(seconds=1), vector=_vector(0.10))
    session.commit()

    result = find_similar(
        session,
        sighting_id=anchor,
        camera_ids=list(cams.values()),
        from_=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=2),
        **SPEEDS,
    )
    assert result.matches == []


def test_browse_offers_only_sightings_that_can_start_a_search(estate: Any) -> None:
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10))
    _sighting(session, cams[refs[0]], NOW + timedelta(minutes=1), vector=None)
    session.commit()

    rows = browse_sightings(
        session,
        camera_ids=list(cams.values()),
        from_=NOW - timedelta(hours=1),
        to=NOW + timedelta(hours=2),
    )
    assert len(rows) == 1
    assert rows[0].has_fingerprint is True


def test_browse_can_filter_by_class(estate: Any) -> None:
    session, cams, refs = estate["session"], estate["cams"], estate["refs"]
    _sighting(session, cams[refs[0]], NOW, vector=_vector(0.10), cls="bus")
    _sighting(session, cams[refs[0]], NOW + timedelta(minutes=1), vector=_vector(0.20), cls="car")
    session.commit()

    window = {"from_": NOW - timedelta(hours=1), "to": NOW + timedelta(hours=2)}
    buses = browse_sightings(session, camera_ids=list(cams.values()), vehicle_class="BUS", **window)
    assert [b.vehicle_class for b in buses] == ["bus"], "the filter must fold case"
