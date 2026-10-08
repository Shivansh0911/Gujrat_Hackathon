"""Record how long a vehicle pass lasted, not just when it began.

`vehicle_sighting` stored `pts_ms` -- the stream position where the pass started -- and
nothing about where it ended. That is enough to place a vehicle in time and enough to
deduplicate an ingest, which is all the table was originally asked for.

It is not enough for dwell or abandonment detection, which is the question "did this
vehicle stay put, and for how long". That question is about the *extent* of a pass, and
the extent was being computed in the pipeline and then discarded at the point of writing.

Three columns, all recoverable from the tracker and none of them derivable afterwards:

* `last_pts_ms` -- stream position of the final frame of the pass. With `pts_ms` this
  gives the duration directly, in the stream's own timebase rather than in wall clock,
  which matters because wall clock on a live feed is an estimate (see `clock_confidence`)
  while PTS is exact.
* `frames_seen` -- how many analytic frames the pass was observed in. A reviewer reading
  a sighting needs to know whether it rests on two frames or thirty, and the existing
  `quality_score` answers a different question: how good the best crop was, not how much
  evidence there is.
* `track_displacement_px` -- how far the box centre moved between the first and last
  frame of the pass, in pixels. Dwell is "a long pass that went nowhere", and without
  displacement a vehicle crossing the frame slowly is indistinguishable from one parked
  in it.

Displacement is stored in pixels rather than metres deliberately. Converting to ground
distance needs camera calibration this estate does not publish -- the same reason
`camera_zone` polygons live in the image plane rather than in geography (migration 0006).
A pixel displacement compared against the vehicle's own box size is scale-free enough to
be useful without pretending to a calibration we do not have.

Defaults are zero rather than NULL for `frames_seen` and `track_displacement_px`, and
`last_pts_ms` defaults to `pts_ms`. Existing rows predate the measurement, so the honest
value is "a pass of unknown extent"; a zero-length, zero-displacement pass is exactly
that and sorts out of every dwell query without needing a NULL check at each call site.

Revision ID: 0008_sighting_pass_extent
Revises: 0007_vehicle_sighting
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_sighting_pass_extent"
down_revision = "0007_vehicle_sighting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vehicle_sighting", sa.Column("last_pts_ms", sa.Float(), nullable=True))
    op.add_column(
        "vehicle_sighting",
        sa.Column("frames_seen", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "vehicle_sighting",
        sa.Column("track_displacement_px", sa.Float(), nullable=False, server_default="0"),
    )

    # Backfill before tightening: a NOT NULL added to a populated table without a value
    # fails, and a server_default on last_pts_ms would be wrong -- there is no sensible
    # constant, only "the same as pts_ms".
    op.execute("UPDATE vehicle_sighting SET last_pts_ms = pts_ms WHERE last_pts_ms IS NULL")
    op.alter_column("vehicle_sighting", "last_pts_ms", nullable=False)

    op.create_check_constraint(
        "ck_sighting_pass_extent",
        "vehicle_sighting",
        "last_pts_ms >= pts_ms AND frames_seen >= 0 AND track_displacement_px >= 0",
    )

    # Dwell queries ask for long passes on one camera. The partial index keeps the
    # common case -- the overwhelming majority of passes, which are brief -- out of it.
    op.execute(
        "CREATE INDEX ix_sighting_dwell ON vehicle_sighting "
        "(camera_id, observed_at_utc) WHERE last_pts_ms - pts_ms > 10000"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sighting_dwell")
    op.drop_constraint("ck_sighting_pass_extent", "vehicle_sighting", type_="check")
    op.drop_column("vehicle_sighting", "track_displacement_px")
    op.drop_column("vehicle_sighting", "frames_seen")
    op.drop_column("vehicle_sighting", "last_pts_ms")
