"""`/readyz` and `/build-info`: what a government ops team will actually monitor.

Liveness is not readiness, and conflating them is an outage
------------------------------------------------------------
`/healthz` answers "is this process running". It must stay cheap and must never touch
the database, because an orchestrator reading liveness as unhealthy *restarts* the
container -- so a liveness probe that fails during a database blip turns a recoverable
dependency problem into a restart loop that guarantees downtime.

`/readyz` answers the different question "should this instance receive traffic". It does
touch dependencies, and a load balancer removing an instance on a failed readiness check
is exactly the right response. Two endpoints because they drive two different actions.

What readiness checks, and what it deliberately does not
---------------------------------------------------------
Checked: the database answers, and the schema is at the migration head this build
expects. A process running old code against a newer schema, or newer code against an
un-migrated database, is the failure mode that produces inexplicable column errors
under load, and it is cheap to detect at the door.

Not checked: the government gateway. It is third-party infrastructure that goes down for
hours at a time (DISCOVERY findings 6, 10, 17), and its state is already reported on
`/health/gateway`. Failing readiness on it would take the whole platform out of the load
balancer because somebody else's CDN is having a bad afternoon -- while journey queries,
alerts, exports and the entire console continue to work perfectly from the database.

Build info exists for the question "which code is this?"
---------------------------------------------------------
The commit is baked at image build time, because a container cannot read the git history
of a repository it does not contain. If the build argument was not supplied the field
reads `unknown` rather than being omitted: an ops team scripting against this needs the
key to exist, and a missing commit is itself information.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: Set at image build time via a Docker build argument. `unknown` outside a built image.
COMMIT_ENV = "SETU_GIT_COMMIT"
BUILD_TIME_ENV = "SETU_BUILD_TIME"

#: Platform-provided fallbacks, tried in order after our own variable.
#:
#: An evidence export names the analytics versions that produced it, and `/build-info`
#: is what ties those back to the code. A deployment reporting `commit: unknown` breaks
#: that chain, and it breaks it silently -- nothing fails, the answer is simply
#: unverifiable later, which is the worst moment to discover it.
#:
#: Render and Netlify both inject the deployed commit without being asked, so reading
#: theirs means the tie-back works on a fresh deploy with no configuration at all. Ours
#: still wins when set, because an explicitly stamped build should beat an inferred one.
_COMMIT_FALLBACK_ENVS = ("RENDER_GIT_COMMIT", "COMMIT_REF", "SOURCE_VERSION", "GIT_COMMIT")


def _commit() -> str:
    for name in (COMMIT_ENV, *_COMMIT_FALLBACK_ENVS):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return "unknown"


#: The Alembic revision this build expects. Read from the migrations directory rather
#: than hard-coded, so it cannot drift from the code it ships with.
_EXPECTED_HEAD_CACHE: str | None = None

#: Process start, for uptime. Monotonic would be better for duration but cannot be
#: rendered as a wall-clock start time, and ops wants both.
_STARTED_AT = time.time()


def expected_migration_head() -> str:
    """The newest revision in `migrations/versions`, as this build understands it.

    Derived from the files rather than configured, because a constant would have to be
    remembered on every migration and would be wrong exactly when it mattered.
    """
    global _EXPECTED_HEAD_CACHE
    if _EXPECTED_HEAD_CACHE is not None:
        return _EXPECTED_HEAD_CACHE

    from services.common.paths import BACKEND_ROOT

    # Both assignment forms. Alembic's own template emits annotated assignments
    # (`revision: str = "..."`), and this project has files in each style -- 0002 is
    # annotated, the rest are not. A parser that handled only the bare form silently
    # dropped 0002's down_revision, which made 0001 look like a second head and
    # `/readyz` report a migration mismatch on a perfectly migrated database.
    pattern = re.compile(
        r"^(?P<name>revision|down_revision)\s*(?::[^=]+)?=\s*(?P<value>None|[\"'][^\"']*[\"'])"
    )

    versions = BACKEND_ROOT / "migrations" / "versions"
    revisions: dict[str, str | None] = {}
    for path in versions.glob("*.py"):
        revision: str | None = None
        down: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line.strip())
            if match is None:
                continue
            raw = match.group("value")
            value = None if raw == "None" else raw.strip("\"'")
            if match.group("name") == "revision":
                revision = value
            else:
                down = value
        if revision:
            revisions[revision] = down

    # The head is the revision nothing else points back to.
    pointed_at = {down for down in revisions.values() if down}
    heads = [rev for rev in revisions if rev not in pointed_at]
    _EXPECTED_HEAD_CACHE = heads[0] if len(heads) == 1 else "unknown"
    if len(heads) > 1:
        log.warning("multiple migration heads found: %s", ", ".join(sorted(heads)))
    return _EXPECTED_HEAD_CACHE


@dataclass(frozen=True)
class DependencyCheck:
    name: str
    ok: bool
    detail: str
    #: Milliseconds. Returned because a database that answers in 900 ms is a different
    #: situation from one that answers in 2, and both pass a boolean check.
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
        }


def check_database(session: Session) -> DependencyCheck:
    started = time.monotonic()
    try:
        session.execute(text("SELECT 1"))
        elapsed = (time.monotonic() - started) * 1000.0
        return DependencyCheck("database", True, "reachable", elapsed)
    except Exception as exc:  # noqa: BLE001 -- a readiness probe reports, never raises
        return DependencyCheck(
            "database",
            False,
            # Type only. A connection error's text contains the host, the port and
            # sometimes the user, and /readyz is commonly left unauthenticated.
            f"unreachable ({type(exc).__name__})",
            (time.monotonic() - started) * 1000.0,
        )


def check_migration_head(session: Session) -> DependencyCheck:
    """The schema must be at the revision this build was written against.

    Not merely "a revision exists". Code running against an un-migrated database fails
    later with a missing-column error under load, which is a far more expensive way to
    learn the same fact.
    """
    expected = expected_migration_head()
    try:
        actual = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as exc:  # noqa: BLE001
        return DependencyCheck("migrations", False, f"unreadable ({type(exc).__name__})")

    if not actual:
        return DependencyCheck("migrations", False, "no revision recorded")
    if expected == "unknown":
        # Honest rather than green: the build could not determine its own head, so it
        # cannot assert agreement.
        return DependencyCheck("migrations", False, f"at {actual}; this build's head is unknown")
    if actual != expected:
        return DependencyCheck(
            "migrations",
            False,
            f"database is at {actual}; this build expects {expected}",
        )
    return DependencyCheck("migrations", True, f"at {actual}")


def readiness(session: Session) -> tuple[bool, list[DependencyCheck]]:
    """Should this instance receive traffic?

    Deliberately excludes the government gateway -- see the module docstring.
    """
    checks = [check_database(session), check_migration_head(session)]
    return all(check.ok for check in checks), checks


def build_info() -> dict[str, Any]:
    """Which code is this, and what does it expect of the world around it."""
    from services.analytics.model_ids import (
        ANPR_VERSION,
        APPEARANCE_KIND,
        SIGHTING_VERSION,
        VEHICLE_DETECTOR_MODEL,
    )
    from services.api.versioning import API_VERSION, SUPPORTED_VERSIONS

    return {
        "service": "setu-api",
        "api_version": API_VERSION,
        "supported_api_versions": list(SUPPORTED_VERSIONS),
        # `unknown` rather than absent: an ops team scripting against this needs the key
        # to be there, and a missing commit is itself worth seeing.
        "commit": _commit(),
        "built_at": os.environ.get(BUILD_TIME_ENV, "unknown"),
        "expected_migration_head": expected_migration_head(),
        "python": platform.python_version(),
        "started_at_epoch": int(_STARTED_AT),
        "uptime_s": int(time.time() - _STARTED_AT),
        "analytics": {
            "anpr": ANPR_VERSION,
            "sighting": SIGHTING_VERSION,
            "vehicle_detector": VEHICLE_DETECTOR_MODEL,
            "appearance_kind": APPEARANCE_KIND,
        },
    }


__all__ = [
    "BUILD_TIME_ENV",
    "COMMIT_ENV",
    "DependencyCheck",
    "build_info",
    "check_database",
    "check_migration_head",
    "expected_migration_head",
    "readiness",
]
