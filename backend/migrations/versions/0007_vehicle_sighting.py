"""Vehicle sightings: identity that does not require a legible plate.

Why this table exists rather than more columns on `detection`
------------------------------------------------------------
`detection.plate_raw` and `detection.plate_normalised` are both `nullable=False`, so a
detection cannot exist without a plate string. On the government estate that is a hard
ceiling on what the platform can know: DISCOVERY finding 22 measured one grammar-valid
registration per ~3,900 frames across 25 live cameras. Every other vehicle those
cameras saw is discarded before it reaches the database.

Relaxing `plate_raw` to nullable was the other option and was rejected. Half the
codebase reads `detection` as "a plate read" -- the watchlist matcher, the speed
flagger, the journey reconstruction, the evidence exporter -- and a nullable plate would
make every one of those silently start handling rows it was never written for. A
separate table keeps `detection` exactly what it says it is and lets a sighting stand on
its own.

`plate_detection_id` is nullable, and that is the entire point of the design: a sighting
is recorded whether or not OCR produced anything, and the plate read is an *attribute* a
sighting may acquire rather than its identity.

Three decisions worth stating, because each was forced by something
-------------------------------------------------------------------
**pgvector stops being optional here.** Migration 0001 enables `vector` inside a
SAVEPOINT and calls it "not yet built", so a deployment without the extension currently
proceeds. From this migration it is required, and a database lacking it fails with a
message naming it. The alternative -- creating the table without the vector column and
degrading silently -- would produce a deployment where the flagship capability is absent
with nothing on screen to say so. Both target environments were checked before
committing to this: Render's managed Postgres 16.15 and the compose
`timescaledb-ha:pg16.4` image both carry pgvector 0.8.0.

**There is no foreign key to `detection`.** TimescaleDB does not support foreign keys
*referencing* a hypertable, and `detection` is one. The reference is therefore soft:
`plate_detection_id` plus `plate_detection_at`, which together are `detection`'s
composite primary key, with an index but no constraint. Note that when TimescaleDB is
absent `detection` is a plain table and a real FK would be accepted -- the schema is
kept identical in both cases anyway, because a constraint that exists only on some
deployments is worse than one that exists on none.

**`appearance_vector` and `appearance_kind` are both-or-neither.** Cosine distance
between two different feature layouts is a number with no meaning, so a stored vector
must always say which layout produced it. Enforced as a CHECK rather than a convention,
for the same reason `camera` has `ck_camera_geom_matches_source`: an unrelated code path
should not be able to leave one of the pair set.

The hypertable is created while the table is empty, as 0003 does and for the reason
0003 gives.

Revision ID: 0007_vehicle_sighting
Revises: 0006_camera_zones
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_vehicle_sighting"
down_revision = "0006_camera_zones"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

#: Dimension of `services.analytics.appearance.DESCRIPTOR_DIM` at the time this
#: migration was written: 64 chroma bins + 4 lightness bins + 8 edge-orientation bins.
#:
#: Hard-coded rather than imported. A migration describes a historical schema state, so
#: it must not change meaning when the descriptor is revised -- importing the constant
#: would silently rewrite history the next time the layout changes. Drift between the
#: two is caught by a test instead (`test_vehicle_sighting_schema.py`), and a layout
#: change ships as a new migration plus a new `appearance_kind`.
APPEARANCE_DIM = 76

_ADMIN = "current_setting('setu.is_admin', true) = 'on'"
_DEPT = "current_setting('setu.department_id', true)"


def upgrade() -> None:
    # Required from here. 0001 treats `vector` as optional, so a database that skipped
    # it reaches this point without the type and would otherwise fail on an opaque
    # "type vector does not exist" from the ALTER below.
    installed = (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        .scalar()
    )
    if not installed:
        raise RuntimeError(
            "the required PostgreSQL extension 'vector' (pgvector) is unavailable. "
            "Appearance-based vehicle identity cannot be stored without it. Run "
            "CREATE EXTENSION vector; as a superuser and re-run the migration."
        )

    op.create_table(
        "vehicle_sighting",
        sa.Column("id", sa.UUID(), nullable=False),
        # Part of the PK so Timescale can partition on it, as `detection` does.
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        # NOT NULL: a sighting *is* a box. A row without one records nothing.
        sa.Column("vehicle_bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("vehicle_class", sa.String(length=32), nullable=False),
        sa.Column("vehicle_class_confidence", sa.Float(), nullable=False),
        # Groups the frames of one pass of one vehicle past one camera. Scoped to the
        # camera, never global: trackers restart on a scene cut (see scene_cut.py) and a
        # globally unique id would imply continuity across that reset.
        sa.Column("track_id", sa.String(length=64), nullable=False),
        # Soft reference to detection(id, observed_at_utc). See the module docstring.
        sa.Column("plate_detection_id", sa.UUID(), nullable=True),
        sa.Column("plate_detection_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dominant_colour_lab", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("appearance_kind", sa.String(length=64), nullable=True),
        # Scalars the scorer uses but which are deliberately NOT in the compared vector,
        # because they describe the framing rather than the vehicle. See appearance.py.
        sa.Column("aspect_ratio", sa.Float(), nullable=False),
        sa.Column("rel_area", sa.Float(), nullable=False),
        sa.Column("rel_cx", sa.Float(), nullable=False),
        sa.Column("rel_cy", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("crop_path", sa.Text(), nullable=True),
        sa.Column("pts_ms", sa.Float(), nullable=False),
        sa.Column("clock_confidence", sa.Float(), nullable=False),
        sa.Column(
            "ingested_at_utc",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1", name="ck_sighting_quality"
        ),
        sa.CheckConstraint(
            "vehicle_class_confidence >= 0 AND vehicle_class_confidence <= 1",
            name="ck_sighting_class_conf",
        ),
        sa.CheckConstraint(
            "clock_confidence >= 0 AND clock_confidence <= 1", name="ck_sighting_clock_conf"
        ),
        # A plate reference is both columns or neither: half of a composite key cannot
        # resolve to a detection.
        sa.CheckConstraint(
            "(plate_detection_id IS NULL AND plate_detection_at IS NULL) OR "
            "(plate_detection_id IS NOT NULL AND plate_detection_at IS NOT NULL)",
            name="ck_sighting_plate_ref_complete",
        ),
        sa.ForeignKeyConstraint(["camera_id"], ["camera.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", "observed_at_utc"),
    )

    # The vector column is added separately: Alembic has no built-in type for it, and
    # going through pgvector's SQLAlchemy type here would make the migration depend on
    # the Python package's own versioning for a column shape that must never change.
    op.execute(f"ALTER TABLE vehicle_sighting ADD COLUMN appearance_vector vector({APPEARANCE_DIM})")
    op.execute(
        """
        ALTER TABLE vehicle_sighting ADD CONSTRAINT ck_sighting_vector_kind_paired
        CHECK (
            (appearance_vector IS NULL AND appearance_kind IS NULL)
            OR (appearance_vector IS NOT NULL AND appearance_kind IS NOT NULL)
        )
        """
    )

    # --- Hypertable, while empty (0003's reasoning applies unchanged) --------------
    timescale = (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        .scalar()
    )
    if timescale:
        op.execute(
            """
            SELECT create_hypertable(
                'vehicle_sighting',
                'observed_at_utc',
                chunk_time_interval => INTERVAL '1 day',
                if_not_exists => TRUE,
                migrate_data => FALSE
            )
            """
        )
    else:
        log.warning(
            "timescaledb is not installed; leaving 'vehicle_sighting' as a plain table. "
            "Correctness is unaffected; time-window queries will not benefit from "
            "chunk exclusion at scale."
        )

    # --- Indexes -------------------------------------------------------------------
    op.create_index(
        "ix_sighting_camera_time",
        "vehicle_sighting",
        ["camera_id", "observed_at_utc"],
        unique=False,
    )
    # One pass of one vehicle: the tracker's frames are read back together constantly,
    # to pick the best crop of a pass and to avoid counting one vehicle many times.
    op.create_index(
        "ix_sighting_track",
        "vehicle_sighting",
        ["camera_id", "track_id", "observed_at_utc"],
        unique=False,
    )
    # Partial: most sightings have no plate, so indexing the nulls would roughly triple
    # the index for rows that are never looked up through it.
    op.execute(
        "CREATE INDEX ix_sighting_plate_detection ON vehicle_sighting "
        "(plate_detection_id) WHERE plate_detection_id IS NOT NULL"
    )

    # HNSW with cosine, matching `appearance.cosine_distance` and the `<=>` operator.
    #
    # HNSW rather than IVFFlat because IVFFlat must be built against a populated table
    # to choose its lists and would have to be rebuilt as the estate grows; HNSW is
    # incremental, which suits a table that starts empty and is appended to forever.
    #
    # Partial on NOT NULL for the same reason as above, and because pgvector will not
    # index a NULL anyway.
    op.execute(
        "CREATE INDEX ix_sighting_appearance_hnsw ON vehicle_sighting "
        "USING hnsw (appearance_vector vector_cosine_ops) "
        "WHERE appearance_vector IS NOT NULL"
    )

    # --- Row-level security, identical in shape to 0005's VIA_CAMERA policies -------
    op.execute("ALTER TABLE vehicle_sighting ENABLE ROW LEVEL SECURITY")
    # Without FORCE the owning role bypasses the policy, and the owning role is the
    # one the application uses. 0005's docstring has the full reasoning.
    op.execute("ALTER TABLE vehicle_sighting FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY vehicle_sighting_department_isolation ON vehicle_sighting
        USING (
            {_ADMIN}
            OR EXISTS (
                SELECT 1 FROM camera c
                WHERE c.id = vehicle_sighting.camera_id
                  AND c.department_id::text = {_DEPT}
            )
        )
        WITH CHECK (
            {_ADMIN}
            OR EXISTS (
                SELECT 1 FROM camera c
                WHERE c.id = vehicle_sighting.camera_id
                  AND c.department_id::text = {_DEPT}
            )
        )
        """
    )


def downgrade() -> None:
    # Dropping the table takes its policy, indexes and constraints with it, so unlike
    # 0003 there is nothing to reconstruct by name. The policy is dropped explicitly
    # first anyway: on a Postgres where the table drop is blocked by a dependency, a
    # half-applied downgrade that left a policy behind would make the re-upgrade fail
    # on "policy already exists", which is a confusing way to learn what went wrong.
    op.execute(
        "DROP POLICY IF EXISTS vehicle_sighting_department_isolation ON vehicle_sighting"
    )
    op.execute("DROP INDEX IF EXISTS ix_sighting_appearance_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_sighting_plate_detection")
    op.drop_index("ix_sighting_track", table_name="vehicle_sighting")
    op.drop_index("ix_sighting_camera_time", table_name="vehicle_sighting")
    op.drop_table("vehicle_sighting")
