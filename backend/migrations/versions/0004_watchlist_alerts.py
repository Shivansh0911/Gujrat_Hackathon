"""Watchlist attributes and alert sighting groups.

Adds what watchlist matching and alert grouping need:

* Vehicle attributes (make, model, colour) so a plate match can be **corroborated**
  rather than trusted alone. A fuzzy plate match that also agrees on colour is a
  materially stronger claim than one that does not.
* `expires_at` is made NOT NULL. A watchlist entry without an expiry becomes a
  permanent shadow record on a citizen -- the entry outlives the investigation that
  justified it and nobody ever revisits it. Requiring the column is the only reliable
  way to prevent that, so existing rows are backfilled rather than left null.
* `alert.sightings` holds the ordered observations behind a movement alert, so
  successive sightings of one vehicle are one developing event rather than a stream
  of near-identical alerts an operator learns to ignore.

Revision ID: 0004_watchlist_alerts
Revises: 0003_detection_hypertable
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_watchlist_alerts"
down_revision = "0003_detection_hypertable"
branch_labels = None
depends_on = None

# Default validity for entries that predate the expiry requirement. Deliberately
# short: an entry nobody can justify should lapse, not persist by inertia.
_BACKFILL_INTERVAL = "90 days"


def upgrade() -> None:
    op.add_column("watchlist_entry", sa.Column("make", sa.String(64), nullable=True))
    op.add_column("watchlist_entry", sa.Column("model", sa.String(64), nullable=True))
    op.add_column("watchlist_entry", sa.Column("colour", sa.String(32), nullable=True))
    op.add_column("watchlist_entry", sa.Column("authority", sa.String(200), nullable=True))
    op.add_column(
        "watchlist_entry",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
    )

    # Backfill before the NOT NULL, so the migration is safe on a populated table.
    op.execute(
        f"UPDATE watchlist_entry SET valid_to = now() + INTERVAL '{_BACKFILL_INTERVAL}' "
        "WHERE valid_to IS NULL"
    )
    op.alter_column("watchlist_entry", "valid_to", nullable=False)

    op.add_column(
        "alert",
        sa.Column(
            "sightings",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "alert",
        sa.Column("is_movement", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "alert",
        sa.Column(
            "corroboration",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_index("ix_watchlist_active", "watchlist_entry", ["active", "valid_to"])


def downgrade() -> None:
    op.drop_index("ix_watchlist_active", table_name="watchlist_entry")
    op.drop_column("alert", "corroboration")
    op.drop_column("alert", "is_movement")
    op.drop_column("alert", "sightings")
    # Nullable again first, so the column returns to exactly its previous definition.
    op.alter_column("watchlist_entry", "valid_to", nullable=True)
    op.drop_column("watchlist_entry", "priority")
    op.drop_column("watchlist_entry", "authority")
    op.drop_column("watchlist_entry", "colour")
    op.drop_column("watchlist_entry", "model")
    op.drop_column("watchlist_entry", "make")
