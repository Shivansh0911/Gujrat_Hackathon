"""Database URL normalisation, and the deployment mistake it exists to prevent.

Every managed Postgres emits a libpq URL -- `postgres://` or `postgresql://` -- and
so does every platform's convenience variable for referencing the database. SQLAlchemy
reads the scheme as a dialect+driver selector and takes bare `postgresql://` to mean
psycopg2, which this project does not install. The failure is a ModuleNotFoundError at
the first connection: after the container has reported healthy, and several layers away
from the environment variable that caused it.
"""

from __future__ import annotations

import pytest

from services.common.dburl import normalise_pg_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The two libpq forms a platform hands out.
        (
            "postgresql://u:p@h:5432/db",
            "postgresql+psycopg://u:p@h:5432/db",
        ),
        (
            "postgres://u:p@h:5432/db",
            "postgresql+psycopg://u:p@h:5432/db",
        ),
        # Already correct: unchanged.
        (
            "postgresql+psycopg://u:p@h/db",
            "postgresql+psycopg://u:p@h/db",
        ),
        # A driver the caller chose deliberately is never overridden.
        (
            "postgresql+asyncpg://u:p@h/db",
            "postgresql+asyncpg://u:p@h/db",
        ),
        # Query parameters (sslmode is common on managed Postgres) survive.
        (
            "postgresql://u:p@h:5432/db?sslmode=require",
            "postgresql+psycopg://u:p@h:5432/db?sslmode=require",
        ),
        # An IPv6 literal host, which a platform's private network can hand out.
        (
            "postgres://u:p@[fd12::3]:5432/db",
            "postgresql+psycopg://u:p@[fd12::3]:5432/db",
        ),
    ],
)
def test_scheme_is_normalised(raw: str, expected: str) -> None:
    assert normalise_pg_url(raw) == expected


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_input_is_returned_unchanged(empty: str | None) -> None:
    """Callers check for absence themselves; this must not invent a URL."""
    assert normalise_pg_url(empty) == empty


def test_surrounding_whitespace_is_stripped() -> None:
    """A value pasted into a platform's variable editor often carries a newline."""
    assert normalise_pg_url("  postgres://u:p@h/db\n") == "postgresql+psycopg://u:p@h/db"


def test_a_non_postgres_url_is_left_alone() -> None:
    assert normalise_pg_url("sqlite:///local.db") == "sqlite:///local.db"


def test_password_containing_a_scheme_like_substring_is_not_rewritten() -> None:
    """Only the leading scheme is considered, never a match further in the string."""
    raw = "postgresql+psycopg://u:postgres://weird@h/db"
    assert normalise_pg_url(raw) == raw
