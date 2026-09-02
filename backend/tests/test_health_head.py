"""Uptime monitors send HEAD. The probes must answer it.

UptimeRobot's default probe is a HEAD request. FastAPI does not add HEAD to a route
declared with `.get()`, so every probe came back 405 and the monitor recorded a
four-day outage on a service that was up the whole time -- a false alarm loud enough
to hide a real one.

These assert the route table, not a live response, so the guarantee holds without a
database or a configured gateway behind it.
"""

from __future__ import annotations

from services.api.routers.system import router


def _methods(path: str) -> set[str]:
    for route in router.routes:
        if getattr(route, "path", None) == path:
            return set(getattr(route, "methods", set()))
    raise AssertionError(f"no route registered at {path}")


def test_liveness_answers_head_as_well_as_get() -> None:
    assert {"GET", "HEAD"} <= _methods("/healthz")


def test_the_root_route_answers_head_too() -> None:
    """Monitors are as likely to be pointed at the root as at the probe path."""
    from services.api.main import app

    for route in app.routes:
        if getattr(route, "path", None) == "/":
            assert {"GET", "HEAD"} <= set(getattr(route, "methods", set()))
            return
    raise AssertionError("no route registered at /")
