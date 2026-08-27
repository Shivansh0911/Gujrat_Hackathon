"""Normalise a PostgreSQL URL into the driver form SQLAlchemy needs.

Deliberately importless beyond the standard library: the Alembic environment and the
container entrypoint both need this before anything heavy is loaded.

Every managed Postgres hands out a URL in libpq form -- `postgres://` or
`postgresql://` -- and every platform's "reference the database" convenience variable
(`${{Postgres.DATABASE_URL}}` on Railway, `DATABASE_URL` on Heroku and Render) is that
form. SQLAlchemy reads the scheme as a *dialect+driver* selector, so it takes bare
`postgresql://` to mean psycopg2, which this project does not install. The result is a
`ModuleNotFoundError: No module named 'psycopg2'` at the first connection, several
layers away from the environment variable that actually caused it -- and it happens
after the container reports healthy, because nothing connects until a request arrives.

Rewriting the scheme here means the platform's own variable can be pasted in
unmodified, which removes the single most likely way to misconfigure a deployment.
An explicit driver the caller has already chosen is never overridden.
"""

from __future__ import annotations

#: libpq-style schemes that carry no SQLAlchemy driver.
_BARE_SCHEMES = ("postgresql://", "postgres://")

#: The driver this project installs. psycopg 3, not psycopg2.
_DRIVER_SCHEME = "postgresql+psycopg://"


def normalise_pg_url(url: str | None) -> str | None:
    """Return `url` with a driver-qualified scheme, or `None`/unchanged input.

    >>> normalise_pg_url("postgres://u:p@h:5432/db")
    'postgresql+psycopg://u:p@h:5432/db'
    >>> normalise_pg_url("postgresql+asyncpg://u:p@h/db")
    'postgresql+asyncpg://u:p@h/db'
    """
    if not url:
        return url
    stripped = url.strip()
    # `postgresql+anything://` already names a driver; respect the caller's choice.
    if stripped.startswith("postgresql+"):
        return stripped
    for scheme in _BARE_SCHEMES:
        if stripped.startswith(scheme):
            return _DRIVER_SCHEME + stripped[len(scheme) :]
    return stripped


__all__ = ["normalise_pg_url"]
