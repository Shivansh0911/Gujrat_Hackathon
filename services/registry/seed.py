"""Seed departments and load the camera coordinate file.

Idempotent: running it twice changes nothing. A seed that only works on an empty
database is a seed nobody runs during a demo.

`geom_source='unset'` rows load with NULL geometry and stay that way. They are
excluded from spatial queries and surfaced as `coordinate missing`, never silently
dropped -- an operator must be able to see that a camera could have contributed
evidence and did not.
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import func, select  # noqa: E402

from services.api.db import get_sessionmaker  # noqa: E402
from services.common import redact  # noqa: E402
from services.registry.enums import CameraStatus, GeomSource, SourceType  # noqa: E402
from services.registry.models import Camera, Department  # noqa: E402

log = logging.getLogger("seed")

SEED_CSV = REPO_ROOT / "data" / "seed" / "camera_geo.csv"

# The five departments present in the evaluation dataset, plus HOME as the default
# owner for gateway cameras whose department the catalogue does not state.
DEPARTMENTS = [
    ("HOME", "Home Department (Police)"),
    ("HEALTH", "Department of Health"),
    ("GSRTC", "Gujarat State Road Transport Corporation"),
    ("PANCHAYAT", "Panchayat, Rural Housing and Rural Development"),
    ("MUNICIPAL", "Municipal Corporation"),
]



class SeedValidationError(ValueError):
    """The coordinate seed file is malformed. Raised before anything is written."""


def _validate_rows(rows: list[dict[str, str]]) -> None:
    """Reject a malformed seed file loudly, before any camera is placed.

    The failure this guards against is silent and expensive: an unquoted comma in
    `location_text` shifts every subsequent field, so a latitude lands in
    `geom_source` and a camera is placed at a coordinate that belongs to nothing.
    That produces an authoritative-looking route through a position no camera
    occupies -- worse than having no coordinate at all, because nothing looks wrong.

    Validating on parse means a bad file stops the seed rather than corrupting the
    registry.
    """
    permitted = {g.value for g in GeomSource} | {"published", "geocoded"}
    problems: list[str] = []

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        ref = (row.get("camera_ref") or "").strip()
        source = (row.get("geom_source") or "").strip()
        lat, lon = (row.get("lat") or "").strip(), (row.get("lon") or "").strip()

        if not ref:
            problems.append(f"line {i}: empty camera_ref")
        if source not in permitted:
            problems.append(
                f"line {i} (camera {ref}): geom_source {source!r} is not one of "
                f"{sorted(permitted)} -- a shifted column is the usual cause"
            )
            continue

        has_coords = bool(lat and lon)
        if source == GeomSource.UNSET.value and has_coords:
            problems.append(f"line {i} (camera {ref}): geom_source=unset but coordinates present")
        if source != GeomSource.UNSET.value and not has_coords:
            problems.append(f"line {i} (camera {ref}): geom_source={source} but no coordinates")

        for name, value, lo, hi in (("lat", lat, -90, 90), ("lon", lon, -180, 180)):
            if not value:
                continue
            try:
                number = float(value)
            except ValueError:
                problems.append(f"line {i} (camera {ref}): {name}={value!r} is not a number")
                continue
            if not lo <= number <= hi:
                problems.append(f"line {i} (camera {ref}): {name}={number} out of range")

        radius = (row.get("confidence_radius_m") or "").strip()
        if radius:
            try:
                if float(radius) <= 0:
                    problems.append(f"line {i} (camera {ref}): confidence_radius_m must be > 0")
            except ValueError:
                problems.append(f"line {i} (camera {ref}): confidence_radius_m={radius!r} invalid")

    if problems:
        detail = chr(10) + "  " + (chr(10) + "  ").join(problems)
        raise SeedValidationError(f"{SEED_CSV} is malformed; refusing to seed:{detail}")


def seed_departments(session) -> dict[str, Department]:
    out: dict[str, Department] = {}
    for code, name in DEPARTMENTS:
        dept = session.execute(
            select(Department).where(Department.code == code)
        ).scalar_one_or_none()
        if dept is None:
            dept = Department(code=code, name=name)
            session.add(dept)
            session.flush()
            log.info("created department %s", code)
        out[code] = dept
    return out


def load_camera_geo(session, departments: dict[str, Department]) -> dict[str, int]:
    """Create or update cameras from the coordinate seed file."""
    if not SEED_CSV.exists():
        log.warning("no seed file at %s", SEED_CSV)
        return {}

    rows = list(csv.DictReader(SEED_CSV.open(encoding="utf-8")))
    _validate_rows(rows)
    counts = {"created": 0, "updated": 0, "unset": 0}
    home = departments["HOME"]

    for row in rows:
        ref = row["camera_ref"].strip()
        if not ref:
            continue

        camera = session.execute(
            select(Camera).where(
                Camera.camera_ref == ref, Camera.source_type == SourceType.GATEWAY.value
            )
        ).scalar_one_or_none()

        created = camera is None
        if camera is None:
            camera = Camera(
                camera_ref=ref,
                name=f"Camera {ref}",
                location_text=row.get("location_text", ""),
                department_id=home.id,
                source_type=SourceType.GATEWAY.value,
                status=CameraStatus.DRAFT.value,
            )
            session.add(camera)

        camera.location_text = row.get("location_text", camera.location_text)

        # A manual survey is a human decision and outranks anything in this file.
        if camera.geom_source == GeomSource.MANUAL_SURVEY.value:
            log.info("camera %s keeps its manual_survey coordinate", ref)
            counts["updated"] += 1
            continue

        lat, lon = row.get("lat", "").strip(), row.get("lon", "").strip()
        source = (row.get("geom_source") or GeomSource.UNSET.value).strip()

        if lat and lon and source != GeomSource.UNSET.value:
            camera.geom = func.ST_SetSRID(func.ST_MakePoint(float(lon), float(lat)), 4326)
            camera.geom_source = source
            radius = row.get("confidence_radius_m", "").strip()
            camera.confidence_radius_m = float(radius) if radius else None
            camera.resolved_by = row.get("resolved_by") or None
            resolved_at = row.get("resolved_at", "").strip()
            camera.resolved_at = (
                datetime.fromisoformat(resolved_at) if resolved_at else datetime.now(timezone.utc)
            )
        else:
            # Explicitly NULL, not zero and not a nearby guess. The CHECK constraint
            # on the table enforces that this pairs with geom_source='unset'.
            camera.geom = None
            camera.geom_source = GeomSource.UNSET.value
            camera.confidence_radius_m = None
            camera.resolved_by = None
            camera.resolved_at = None
            counts["unset"] += 1

        counts["created" if created else "updated"] += 1

    session.flush()
    return counts


def main() -> int:
    redact.install(level=logging.INFO)
    session = get_sessionmaker()()
    try:
        departments = seed_departments(session)
        counts = load_camera_geo(session, departments)
        session.commit()
    finally:
        session.close()

    print("\nSeed complete")
    print(f"  departments : {len(DEPARTMENTS)}")
    print(f"  cameras     : {counts.get('created', 0)} created, "
          f"{counts.get('updated', 0)} updated")
    print(f"  coordinate missing: {counts.get('unset', 0)} "
          f"(excluded from spatial queries, shown in the UI)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
