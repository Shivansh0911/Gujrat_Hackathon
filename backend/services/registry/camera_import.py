"""Row-level validation for camera onboarding, shared by the seed script and the API.

Model 1 requires that cameras can be onboarded in bulk as well as one at a time. Both
paths must agree on what a valid row is, so the rules live here once rather than being
implemented twice and drifting -- a bulk import that accepted a row the seed script
rejects (or the reverse) would put two different registries in front of the same jury.

The two callers differ only in what they do with the problems, and that difference is
deliberate:

* **The seed script is all-or-nothing.** It runs against a file we control, at a point
  where a malformed row means the file itself is wrong -- most often an unquoted comma
  in `location_text` shifting every later field, so a latitude lands in `geom_source`
  and a camera is placed somewhere no camera is. Importing the good half of such a file
  would leave the registry quietly wrong.

* **The API import is row-by-row.** It runs against a file an operator just picked, and
  the useful answer there is "these 28 landed, these 2 did not, here is why" rather
  than a single rejection with no way to see which line to fix.

So this module reports problems and takes no view on what to do about them.
"""

from __future__ import annotations

from services.registry.enums import GeomSource

#: `published` and `geocoded` are historical values still present in committed seed
#: data; they are accepted on input and normalised by the caller.
PERMITTED_GEOM_SOURCES = {g.value for g in GeomSource} | {"published", "geocoded"}

#: The columns a row must supply. Everything else is optional metadata.
REQUIRED_COLUMNS = ("camera_ref", "geom_source")


def validate_row(row: dict[str, str], line_no: int) -> list[str]:
    """Return every problem with one row. An empty list means the row is usable.

    `line_no` is the line in the source file, counting the header as line 1, so a
    message points at something the operator can actually open and look at.
    """
    problems: list[str] = []

    ref = (row.get("camera_ref") or "").strip()
    source = (row.get("geom_source") or "").strip()
    lat = (row.get("lat") or "").strip()
    lon = (row.get("lon") or "").strip()

    if not ref:
        problems.append(f"line {line_no}: empty camera_ref")

    if source not in PERMITTED_GEOM_SOURCES:
        # A shifted column is the usual cause, and saying so saves the reader from
        # hunting for a typo that is not there.
        problems.append(
            f"line {line_no} (camera {ref}): geom_source {source!r} is not one of "
            f"{sorted(PERMITTED_GEOM_SOURCES)} -- a shifted column is the usual cause"
        )
        # Later checks key off the source, so there is nothing further to say.
        return problems

    has_coords = bool(lat and lon)
    if source == GeomSource.UNSET.value and has_coords:
        problems.append(f"line {line_no} (camera {ref}): geom_source=unset but coordinates present")
    if source != GeomSource.UNSET.value and not has_coords:
        problems.append(f"line {line_no} (camera {ref}): geom_source={source} but no coordinates")

    for name, value, lo, hi in (("lat", lat, -90.0, 90.0), ("lon", lon, -180.0, 180.0)):
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            problems.append(f"line {line_no} (camera {ref}): {name}={value!r} is not a number")
            continue
        if not lo <= number <= hi:
            problems.append(f"line {line_no} (camera {ref}): {name}={number} out of range")

    radius = (row.get("confidence_radius_m") or "").strip()
    if radius:
        try:
            if float(radius) <= 0:
                problems.append(f"line {line_no} (camera {ref}): confidence_radius_m must be > 0")
        except ValueError:
            problems.append(
                f"line {line_no} (camera {ref}): confidence_radius_m={radius!r} invalid"
            )

    return problems


def validate_rows(rows: list[dict[str, str]]) -> list[str]:
    """Every problem across every row, for a caller that wants all-or-nothing."""
    problems: list[str] = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        problems.extend(validate_row(row, i))
    return problems


__all__ = ["PERMITTED_GEOM_SOURCES", "REQUIRED_COLUMNS", "validate_row", "validate_rows"]
