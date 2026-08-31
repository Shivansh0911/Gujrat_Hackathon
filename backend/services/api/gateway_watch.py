"""Passive watch on the government gateway, so an outage is visible before it is asked about.

Gateway reachability was only ever established when someone pressed **Compare with
gateway**. That is the wrong moment to find out. This feed has been measured at 25 of
30 cameras (2026-08-27), 18 of 30 (2026-08-30) and a complete Cloudflare 502 across
every endpoint (2026-08-31), and each of those readings was taken by hand. During a
live demonstration the interesting question is not "is it up" but "when did it stop",
and nobody can answer that from a button that only reports *now*.

So this polls, records transitions, and the console shows the answer without being
asked. Three things it deliberately does:

**It reuses `fetch_catalogue`.** That is the same call `sync-catalogue` makes and the
same endpoint the ingest path depends on, so "reachable" here means the same thing it
means everywhere else. A second, cheaper probe -- a bare TCP connect, say -- would
report a Cloudflare edge that is perfectly healthy while the origin behind it is not,
which is exactly the failure in front of us.

**It records the transition, not just the state.** `unreachable_since` is set once,
when reachable becomes unreachable, and cleared on recovery. Storing only the current
status would make "since when" unanswerable, which is the question that actually gets
asked.

**It never raises into the request path.** A watcher that can break the page it
informs is worse than no watcher. Every failure is a recorded observation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: How often to ask. Slow enough to be negligible load on infrastructure we do not
#: own -- the same courtesy the ingest pool's jittered backoff extends -- and fast
#: enough that "since when" is accurate to the minute during a demonstration.
POLL_INTERVAL_S = 60.0

#: Bounded so a hung connection cannot stall the poll loop behind it.
PROBE_TIMEOUT_S = 15.0


@dataclass
class GatewayStatus:
    """What we currently believe about the gateway, and when we last checked."""

    reachable: bool | None = None
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    unreachable_since: datetime | None = None
    cameras_in_catalogue: int | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    checks_performed: int = 0

    def as_dict(self) -> dict[str, object]:
        def iso(d: datetime | None) -> str | None:
            return d.isoformat() if d else None

        return {
            "reachable": self.reachable,
            "last_checked_at": iso(self.last_checked_at),
            "last_success_at": iso(self.last_success_at),
            "unreachable_since": iso(self.unreachable_since),
            "cameras_in_catalogue": self.cameras_in_catalogue,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "checks_performed": self.checks_performed,
            "poll_interval_s": POLL_INTERVAL_S,
        }


#: Process-local. This is an observation cache, not state of record -- it is allowed
#: to be empty on a cold start, and the console says "not yet checked" rather than
#: inventing a status.
_status = GatewayStatus()
_lock = asyncio.Lock()


def current_status() -> GatewayStatus:
    return _status


def _probe_once() -> tuple[bool, int | None, str | None]:
    """One synchronous reachability check. Returns (reachable, camera_count, error)."""
    try:
        from services.common.catalogue import fetch_catalogue
        from services.common.config import get_settings
    except Exception as exc:  # noqa: BLE001 -- an import failure is still an answer
        return False, None, f"{type(exc).__name__}: {exc}"

    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001
        # No gateway configured is a different thing from a gateway that is down, and
        # the console distinguishes them. See routers/cameras.py for the same split.
        return False, None, "no gateway configured (SETU_GATEWAY_HOST unset)"

    try:
        descriptors = fetch_catalogue(settings, timeout=PROBE_TIMEOUT_S)
        return True, len(descriptors), None
    except Exception as exc:  # noqa: BLE001 -- third-party infrastructure
        return False, None, f"{type(exc).__name__}: {exc}"


async def check_now() -> GatewayStatus:
    """Probe once and fold the result into the recorded status."""
    reachable, count, error = await asyncio.to_thread(_probe_once)
    now = datetime.now(timezone.utc)

    async with _lock:
        was = _status.reachable
        _status.last_checked_at = now
        _status.checks_performed += 1

        if reachable:
            _status.reachable = True
            _status.last_success_at = now
            _status.cameras_in_catalogue = count
            _status.last_error = None
            _status.consecutive_failures = 0
            if was is False:
                # Worth a line at info: a recovery is the event people wait for.
                log.info("gateway reachable again after %s", _status.unreachable_since)
            _status.unreachable_since = None
        else:
            _status.reachable = False
            _status.last_error = error
            _status.consecutive_failures += 1
            if _status.unreachable_since is None:
                _status.unreachable_since = now
                # Logged once per outage, not once per poll. A minute-by-minute repeat
                # of the same fact buries everything else in the log.
                log.warning("gateway unreachable: %s", error)

    return _status


async def watch_forever() -> None:
    """Poll until cancelled. Never raises out of the loop."""
    log.info("gateway watch started (every %.0fs)", POLL_INTERVAL_S)
    while True:
        try:
            await check_now()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a watcher must not die of its own error
            log.exception("gateway watch iteration failed; continuing")
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise


_task: asyncio.Task[None] | None = None


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(watch_forever())


async def stop() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None


__all__ = [
    "POLL_INTERVAL_S",
    "GatewayStatus",
    "current_status",
    "check_now",
    "start",
    "stop",
]
