"""Convert `detection` to a TimescaleDB hypertable.

Done now, while the table is empty, deliberately. `create_hypertable` on a populated
table requires `migrate_data => true`, which rewrites every row under an exclusive
lock -- acceptable for a demo, unacceptable for an estate accumulating detections from
30+ cameras, and exactly the kind of migration that gets deferred until it cannot be
run at all.

Chunk interval is one day: journey queries scan a bounded window (the test case uses
12 hours), so daily chunks give the planner useful exclusion without producing
thousands of tiny chunks over a demonstration weekend.

Revision ID: 0003_detection_hypertable
Revises: 0002_core_schema
"""

from __future__ import annotations

from alembic import op

revision = "0003_detection_hypertable"
down_revision = "0002_core_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        SELECT create_hypertable(
            'detection',
            'observed_at_utc',
            chunk_time_interval => INTERVAL '1 day',
            if_not_exists => TRUE,
            migrate_data => FALSE
        )
        """
    )


def downgrade() -> None:
    # There is no "un-hypertable" operation. Reversing means materialising the rows
    # into a plain table and swapping it in.
    #
    # Every constraint and index is recreated by name rather than with
    # `LIKE ... INCLUDING ALL`. INCLUDING ALL copies indexes under server-generated
    # names and does not copy foreign keys at all, so the previous migration's
    # downgrade then fails on `index "ix_detection_plate_time" does not exist` and the
    # table silently loses its reference to `camera`. Caught by running the full
    # downgrade/upgrade round trip rather than by assuming reversibility.
    op.execute("CREATE TABLE detection_plain (LIKE detection EXCLUDING ALL)")
    op.execute("INSERT INTO detection_plain SELECT * FROM detection")
    op.execute("DROP TABLE detection")
    op.execute("ALTER TABLE detection_plain RENAME TO detection")

    # Restore exactly what 0002 created, under exactly the names it used.
    op.execute(
        "ALTER TABLE detection ADD CONSTRAINT detection_pkey "
        "PRIMARY KEY (id, observed_at_utc)"
    )
    op.execute(
        "ALTER TABLE detection ADD CONSTRAINT detection_camera_id_fkey "
        "FOREIGN KEY (camera_id) REFERENCES camera(id) ON DELETE RESTRICT"
    )
    op.execute(
        "ALTER TABLE detection ADD CONSTRAINT ck_detection_conf "
        "CHECK (confidence >= 0 AND confidence <= 1)"
    )
    op.execute(
        "CREATE INDEX ix_detection_plate_time ON detection "
        "(plate_normalised, observed_at_utc)"
    )
    op.execute(
        "CREATE INDEX ix_detection_camera_time ON detection (camera_id, observed_at_utc)"
    )
