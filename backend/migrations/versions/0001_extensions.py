"""Enable required Postgres extensions.

Extensions come first and in their own migration so an extension conflict surfaces on
a clean volume immediately, rather than halfway through creating tables that then have
to be rolled back by hand.

TimescaleDB is installed now even though the first hypertable arrives in 0003:
discovering an extension conflict after data exists is expensive, and TimescaleDB in
particular must be present before any table it will later manage is created.

Revision ID: 0001_extensions
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_extensions"
down_revision = None
branch_labels = None
depends_on = None

_EXTENSIONS = (
    "postgis",       # geography(Point,4326), ST_Distance for route plausibility
    "timescaledb",   # detection hypertable
    "vector",        # pgvector, vehicle re-identification embeddings (T2.3)
    "pg_trgm",       # trigram index for fuzzy plate search
    "pgcrypto",      # gen_random_uuid()
)


def upgrade() -> None:
    for ext in _EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")


def downgrade() -> None:
    # Deliberately ordered opposite to creation. postgis is dropped last because other
    # objects may depend on its types.
    for ext in reversed(_EXTENSIONS):
        # RESTRICT (the default) rather than CASCADE: if something still depends on an
        # extension, that is a fact the operator needs to see, not one to silently
        # destroy tables over.
        op.execute(f"DROP EXTENSION IF EXISTS {ext}")
