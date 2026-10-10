#!/usr/bin/env python
"""Move the seeded replay timeline forward, the way a fresh seed would place it.

Why this exists
---------------
`seed_demo.py` places the replay cameras' timeline starting seven hours before the moment
it runs, and `seed_sighting_fixture.py` anchors the vehicle sightings to that. Both are
relative to their run. A deployment seeded six weeks ago therefore holds a demonstration
dated six weeks ago, and every screen in the console defaults to a recent window: the
dashboard opens on 24 hours and "Find a vehicle" on two days. The data is all present and
every query is correct -- and the console shows zeroes, which is indistinguishable from a
platform that does not work.

Re-running the seed is the intended fix and needs a deployment. This does the same
arithmetic against a database directly, for the case where the container cannot be
redeployed in time.

What it moves, and what it must not
-----------------------------------
**Only the rows the demo seed itself placed**: the four `REPLAY-*` cameras' plate
detections, the vehicle sightings on those cameras, and the alerts raised from them. Those
timestamps were never observations of anything -- `seed_demo` assigns them from
`now() - 7h` plus the real travel time between the four cameras.

**Never the government-feed detections.** Those came from the gateway with the time the
camera reported, and that is a record of when something was seen. Moving it would be
falsifying an observation to make a demonstration look fresher, which is the one thing
this repository does not do. They stay where they are, and if that makes them old on the
dashboard, the dashboard is right.

**Never the audit ledger.** It is append-only and hash-chained; rewriting entries would
break the chain, and the chain is the point.

One offset is applied to every row it does touch, so the relative structure is untouched:
the four hops keep their intervals, `pts_ms` still lines up with `observed_at_utc`, and
the plate links stay inside the linker's window.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

#: The cameras the demo seed places. Everything else is left alone.
REPLAY_REFS = ("REPLAY-01", "REPLAY-02", "REPLAY-03", "REPLAY-04")

#: Where `seed_demo.py` puts the first camera's epoch, and therefore where this puts it.
#: Kept as one constant in both places would be better; it is repeated here deliberately
#: so this script states its own assumption rather than importing one that could change
#: under it without the docstring noticing.
HOURS_AGO = 7.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--hours-ago",
        type=float,
        default=HOURS_AGO,
        help="where the first replay camera's timeline should start (default: 7)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the offset and the row counts, change nothing",
    )
    args = ap.parse_args()

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from services.common.dburl import normalise_pg_url

    url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ.get("SETU_DATABASE_URL")
    normalised = normalise_pg_url(url)
    if normalised:
        session = sessionmaker(bind=create_engine(normalised, future=True), future=True)()
    else:
        from services.api.db import get_sessionmaker

        session = get_sessionmaker()()

    from services.api.tenancy import set_admin_context

    set_admin_context(session)

    camera_ids = [
        row[0]
        for row in session.execute(
            text("SELECT id FROM camera WHERE camera_ref = ANY(:refs)"),
            {"refs": list(REPLAY_REFS)},
        )
    ]
    if not camera_ids:
        print("no replay cameras in this registry; nothing to rebase", file=sys.stderr)
        return 0

    # The first camera's epoch is the anchor, because that is the one `seed_demo` places
    # directly; every other camera is an offset from it and moves with it.
    current = session.execute(
        text(
            "SELECT min(d.observed_at_utc) FROM detection d JOIN camera c ON c.id = d.camera_id"
            " WHERE c.camera_ref = 'REPLAY-01'"
        )
    ).scalar()
    if current is None:
        print("no REPLAY-01 detections to anchor to; nothing to rebase", file=sys.stderr)
        return 0

    target = datetime.now(timezone.utc) - timedelta(hours=args.hours_ago)
    offset: timedelta = target - current
    print(f"replay timeline starts   : {current.isoformat()}")
    print(f"it should start          : {target.isoformat()}")
    print(f"offset                   : {offset}")

    counts = {
        table: session.execute(
            text(f"SELECT count(*) FROM {table} WHERE camera_id = ANY(:ids)"),  # noqa: S608
            {"ids": camera_ids},
        ).scalar()
        for table in ("detection", "vehicle_sighting", "alert")
    }
    for table, count in counts.items():
        print(f"  {table:<17}: {count} row(s) on the replay cameras")

    if args.dry_run:
        print("\ndry run; nothing changed")
        return 0

    if abs(offset.total_seconds()) < 60:
        print("\nalready in place; nothing to do")
        return 0

    # One offset, every table, one transaction. `observed_at_utc` is the hypertable's
    # partitioning column, so these rows move between chunks -- supported, and the reason
    # this is an UPDATE of a few hundred rows rather than anything cleverer.
    session.execute(
        text(
            "UPDATE detection SET observed_at_utc = observed_at_utc + :off"
            " WHERE camera_id = ANY(:ids)"
        ),
        {"off": offset, "ids": camera_ids},
    )
    session.execute(
        text(
            "UPDATE vehicle_sighting SET observed_at_utc = observed_at_utc + :off,"
            " plate_detection_at = plate_detection_at + :off"
            " WHERE camera_id = ANY(:ids)"
        ),
        {"off": offset, "ids": camera_ids},
    )
    session.execute(
        text(
            "UPDATE alert SET observed_at_utc = observed_at_utc + :off WHERE camera_id = ANY(:ids)"
        ),
        {"off": offset, "ids": camera_ids},
    )
    session.commit()

    span = session.execute(
        text(
            "SELECT min(d.observed_at_utc), max(d.observed_at_utc) FROM detection d"
            " WHERE d.camera_id = ANY(:ids)"
        ),
        {"ids": camera_ids},
    ).first()
    if span and span[0]:
        print(f"\nreplay detections now span {span[0].isoformat()} .. {span[1].isoformat()}")
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
