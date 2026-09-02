"""Intrusion zones: a polygon per camera, in that camera's image plane.

A zone is the region of a camera's *view* that matters -- a bus depot forecourt, a
restricted gate approach -- and a detection whose vehicle box centres inside it raises
an alert through the ordinary alert path.

Why the polygon is not geographic
---------------------------------
Every other geometry in this schema is `Geography(POINT, 4326)`, because it answers a
question about the world. This one answers a question about a picture: is the vehicle
inside the part of *this frame* the operator drew round? A detection's bounding box is
in frame pixels, and there is no projection from a monocular CCTV frame to ground
coordinates without camera calibration this estate does not publish. Storing the zone
in EPSG:4326 would therefore be a coordinate system chosen for consistency rather than
for meaning, and would invite exactly the mistake of comparing it against real
positions.

So: a plain `geometry(POLYGON)` with SRID 0, in pixel coordinates, plus the frame size
it was drawn against. The reference size is not decoration -- a camera that changes
resolution invalidates the zone, and recording what it was drawn at is what lets that
be detected rather than silently mis-evaluated.

Revision ID: 0006_camera_zones
Revises: 0005_row_level_security
"""

from __future__ import annotations

import geoalchemy2  # autogenerate emits geoalchemy2 types but not this import
import sqlalchemy as sa
from alembic import op

revision = "0006_camera_zones"
down_revision = "0005_row_level_security"
branch_labels = None
depends_on = None

_ADMIN = "current_setting('setu.is_admin', true) = 'on'"
_DEPT = "current_setting('setu.department_id', true)"


def upgrade() -> None:
    op.create_table(
        "camera_zone",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("camera_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "polygon",
            geoalchemy2.types.Geometry(
                geometry_type="POLYGON",
                srid=0,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=False,
        ),
        # The frame the operator drew against. A camera that changes resolution makes
        # the zone wrong, and this is what makes that visible instead of silent.
        sa.Column("reference_width", sa.Integer(), nullable=False),
        sa.Column("reference_height", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["camera_id"], ["camera.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One name per camera, so "re-drawing the gate zone" updates rather than
        # quietly accumulating a second overlapping zone that double-alerts.
        sa.UniqueConstraint("camera_id", "name", name="uq_camera_zone_name"),
    )
    op.create_index("ix_camera_zone_camera", "camera_zone", ["camera_id"], unique=False)
    op.create_index(
        "ix_camera_zone_polygon", "camera_zone", ["polygon"], unique=False, postgresql_using="gist"
    )

    # Same tenancy rule as `detection` and `alert`: the department boundary is reached
    # through `camera`, and FORCE is required or the policy is inert for the very role
    # it exists to constrain. See migration 0005.
    op.execute("ALTER TABLE camera_zone ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE camera_zone FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY camera_zone_department_isolation ON camera_zone
        USING (
            {_ADMIN}
            OR EXISTS (
                SELECT 1 FROM camera c
                WHERE c.id = camera_zone.camera_id
                  AND c.department_id::text = {_DEPT}
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS camera_zone_department_isolation ON camera_zone")
    op.execute("ALTER TABLE camera_zone NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE camera_zone DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_camera_zone_polygon", table_name="camera_zone")
    op.drop_index("ix_camera_zone_camera", table_name="camera_zone")
    op.drop_table("camera_zone")
