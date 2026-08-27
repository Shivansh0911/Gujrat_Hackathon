#!/usr/bin/env python
"""Seed a representative watchlist from plates that genuinely appear in the footage.

The competition explicitly permits our own representative watchlist. Two failure
modes to avoid, and this script is built around both:

* **A demo where nothing matches is a failed demo.** So entries are drawn from plates
  the pipeline actually read, discovered from the `detection` table rather than
  invented.
* **A demo where everything matches is not believable.** So decoy entries are included
  that are valid Indian registrations from other states and will never match this
  footage. A jury seeing 8 detections and 8 alerts learns nothing; seeing 8
  detections, 3 alerts and 5 correctly ignored vehicles learns that the system
  discriminates.

One entry is deliberately seeded as a **near-miss**: a real detected plate with a
single character swapped for its OCR-confusable partner. That entry can only be found
by confusion-aware fuzzy matching, so it demonstrates the capability an exact-match
system does not have.

Usage:
    python scripts/seed_watchlist.py [--reset]
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# backend/ on the path so `services.*` imports resolve however this is launched.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


from sqlalchemy import delete, func, select  # noqa: E402

from services.analytics.plate_grammar import DIGIT_TO_LETTER, LETTER_TO_DIGIT  # noqa: E402
from services.api.db import get_sessionmaker  # noqa: E402
from services.api.tenancy import set_admin_context  # noqa: E402
from services.common import redact  # noqa: E402
from services.registry.models import Detection, WatchlistEntry  # noqa: E402

log = logging.getLogger("watchlist")

# Valid Indian registrations from states that do not appear in the footage. These
# exist to be correctly ignored.
# The authority is shown on every alert card and in the exported evidence PDF, so a
# placeholder token there reads as an unfinished system. These are the units that
# would realistically hold each kind of listing; the `notes` on every seeded entry
# still says plainly that it is representative demonstration data.
AUTHORITY_BY_LIST = {
    "Stolen Vehicles": "Gujarat Police, Crime Branch",
    "Suspect Vehicles": "Gujarat Police, Special Operations Group",
    "Blacklisted": "Gujarat Police, Traffic Enforcement (Ahmedabad City)",
    "Wanted Persons": "Gujarat Police, Crime Branch",
}
DEFAULT_AUTHORITY = "Gujarat Police, Crime Branch"


def _authority(watchlist_name: str, state: str | None = None) -> str:
    """Who holds the listing. Out-of-state decoys are attributed to their own force."""
    if state and state != "Gujarat":
        return f"{state} Police, State Crime Records Bureau"
    return AUTHORITY_BY_LIST.get(watchlist_name, DEFAULT_AUTHORITY)


DECOYS = [
    ("MH12AB4567", "Maharashtra", "Stolen Vehicles", 80, "MH/2026/0912", "white", "Maruti"),
    ("DL08CA1234", "Delhi", "Wanted Persons", 90, "DL/2026/1177", "black", "Hyundai"),
    ("RJ14CV7788", "Rajasthan", "Stolen Vehicles", 70, "RJ/2026/0455", "silver", "Tata"),
    ("TN10BX2211", "Tamil Nadu", "Blacklisted", 40, "TN/2026/0031", "red", "Honda"),
    ("UP32DK9090", "Uttar Pradesh", "Suspect Vehicles", 60, "UP/2026/2210", "blue", "Mahindra"),
]


def _near_miss(plate: str) -> str | None:
    """Substitute one character for its genuine OCR-confusable partner.

    The confusion sets are digit-to-letter, not digit-to-digit: `1` is confused with
    `I`, and `7` with `T`, but `1` and `7` are never confused with each other. An
    earlier version swapped digit pairs directly and produced an entry no fuzzy
    matcher could ever reach, which defeated the point of seeding it.

    The result is intentionally not a grammar-valid registration. It represents the
    watchlist holding a character the OCR reads the other way -- exactly the case
    confusion-aware matching exists to recover.
    """
    chars = list(plate)
    # Work from the numeric tail backwards: that is where a single glyph confusion is
    # most common and least likely to collide with another watchlist entry.
    for i in range(len(chars) - 1, -1, -1):
        partner = DIGIT_TO_LETTER.get(chars[i]) or LETTER_TO_DIGIT.get(chars[i])
        if partner:
            chars[i] = partner
            return "".join(chars)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="remove existing entries first")
    ap.add_argument(
        "--match-count", type=int, default=3, help="how many genuinely-present plates to watchlist"
    )
    args = ap.parse_args()

    redact.install(level=logging.INFO)
    session = get_sessionmaker()()
    # Background job: no human actor, and it legitimately spans departments.
    set_admin_context(session)
    now = datetime.now(timezone.utc)

    try:
        if args.reset:
            session.execute(delete(WatchlistEntry))
            session.flush()
            log.info("cleared existing watchlist entries")

        # Draw from plates the pipeline actually read, strongest reads first, so the
        # demo matches on evidence a reviewer can see rather than on a lucky guess.
        present = list(
            session.execute(
                select(
                    Detection.plate_normalised,
                    func.count().label("sightings"),
                    func.max(Detection.confidence).label("best"),
                )
                .where(func.length(Detection.plate_normalised) >= 9)
                .group_by(Detection.plate_normalised)
                .order_by(func.max(Detection.confidence).desc())
            )
        )
        if not present:
            log.error("no detections in the database; run the ANPR pipeline first")
            return 2

        chosen = present[: args.match_count]
        log.info("watchlisting %d plate(s) that genuinely appear", len(chosen))

        created = 0

        def add(plate, name, source, priority, case_ref, colour, make, notes):
            nonlocal created
            exists = session.execute(
                select(WatchlistEntry).where(WatchlistEntry.plate_normalised == plate)
            ).scalar_one_or_none()
            if exists is not None:
                return
            session.add(
                WatchlistEntry(
                    plate_normalised=plate,
                    entity_type="vehicle",
                    watchlist_name=name,
                    source_system=source,
                    authority=source,
                    severity="high" if priority >= 80 else "medium",
                    priority=priority,
                    case_ref=case_ref,
                    colour=colour,
                    make=make,
                    notes=notes,
                    active=True,
                    valid_from=now - timedelta(days=1),
                    # Every entry expires. A watchlist without expiry becomes a
                    # permanent shadow record that outlives its investigation.
                    valid_to=now + timedelta(days=30),
                )
            )
            created += 1

        for i, row in enumerate(chosen):
            plate = row.plate_normalised
            watchlist_name = ["Stolen Vehicles", "Suspect Vehicles", "Blacklisted"][i % 3]
            add(
                plate,
                watchlist_name,
                _authority(watchlist_name),
                [85, 65, 45][i % 3],
                f"GJ/SETU/2026/{1000 + i}",
                None,
                None,
                f"Representative entry; plate observed {row.sightings} time(s) "
                f"at confidence {row.best:.2f}",
            )

        # Draw the near-miss from a plate NOT already watchlisted exactly, so the
        # only route from footage to alert is the fuzzy matcher. Taking it from an
        # exact entry would let the exact match win and hide the capability.
        remaining = present[args.match_count :]
        base = remaining[0].plate_normalised if remaining else chosen[-1].plate_normalised
        strongest = base
        near = _near_miss(base)
        if near and near != strongest:
            add(
                near,
                "Stolen Vehicles",
                _authority("Stolen Vehicles"),
                75,
                "GJ/SETU/2026/2001",
                None,
                None,
                f"Near-miss of {strongest}: one OCR-confusable character differs. "
                "Only a confusion-aware matcher finds this; exact matching misses it.",
            )
            log.info("near-miss entry %s (from %s)", near, strongest)

        for plate, state, name, priority, case_ref, colour, make in DECOYS:
            add(
                plate,
                name,
                _authority(name, state),
                priority,
                case_ref,
                colour,
                make,
                f"Decoy: valid {state} registration, absent from this footage. "
                "Present so the demo shows discrimination rather than blanket matching.",
            )

        session.commit()

        total = session.execute(select(func.count()).select_from(WatchlistEntry)).scalar_one()
        print(f"\nWatchlist seeded: {created} new entries, {total} total")
        print(f"  {len(chosen)} plate(s) present in footage (will match)")
        print(f"  {'1' if near else '0'} near-miss (fuzzy match only)")
        print(f"  {len(DECOYS)} decoys (must NOT match)\n")
        for entry in session.execute(
            select(WatchlistEntry).order_by(WatchlistEntry.priority.desc())
        ).scalars():
            print(
                f"  {entry.plate_normalised:<12} p={entry.priority:<3} "
                f"{entry.watchlist_name:<18} {entry.case_ref}"
            )
        print()
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
