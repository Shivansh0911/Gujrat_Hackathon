"""Uptime monitors send HEAD. The probes must answer it.

UptimeRobot's default probe is a HEAD request. FastAPI does not add HEAD to a route
declared with `.get()`, so every probe came back 405 and the monitor recorded a
four-day outage on a service that was up the whole time -- a false alarm loud enough
to hide a real one.

These assert the route table, not a live response, so the guarantee holds without a
database or a configured gateway behind it.
"""

from __future__ import annotations

import pytest

from services.api.routers.system import router


def _methods(path: str) -> set[str]:
    for route in router.routes:
        if getattr(route, "path", None) == path:
            return set(getattr(route, "methods", set()))
    raise AssertionError(f"no route registered at {path}")


def test_liveness_answers_head_as_well_as_get() -> None:
    assert {"GET", "HEAD"} <= _methods("/healthz")


def test_the_root_route_answers_head_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monitors are as likely to be pointed at the root as at the probe path.

    Importing the app builds `ApiSettings`, which requires a database URL and a JWT
    secret. CI has neither, so the values are supplied here and the cache is cleared
    on both sides -- otherwise this test either fails on a machine without a `.env`
    (which is how it first went red) or leaks a dummy settings object into whatever
    runs next.
    """
    monkeypatch.setenv("SETU_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/none")
    monkeypatch.setenv("SETU_JWT_SECRET", "not-a-real-secret-only-for-import")

    from services.api.config import get_api_settings

    get_api_settings.cache_clear()
    try:
        from services.api.main import app

        for route in app.routes:
            if getattr(route, "path", None) == "/":
                assert {"GET", "HEAD"} <= set(getattr(route, "methods", set()))
                return
        raise AssertionError("no route registered at /")
    finally:
        get_api_settings.cache_clear()


def test_media_routes_answer_head_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checking whether an evidence file exists must not cost 22 MB of video.

    The own-feed clip is that big, and a HEAD against it returned 405 while GET
    returned `200 video/mp4` -- so the responsive audit's own probe concluded the
    video was broken. Same omission as `/healthz`, one route family later.
    """
    monkeypatch.setenv("SETU_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/none")
    monkeypatch.setenv("SETU_JWT_SECRET", "not-a-real-secret-only-for-import")

    from services.api.config import get_api_settings

    get_api_settings.cache_clear()
    try:
        from services.api.main import app

        wanted = {
            "/media/crops/{name}",
            "/media/own-feed/{name}",
            "/media/gateway/{token}",
        }

        # `app.routes` does not flatten: an included router is kept as a wrapper
        # object with the real routes underneath it. Reading only the top level made
        # this test report the gateway proxy as unregistered while it was serving
        # traffic on the deployed instance -- the test's model of the route table was
        # wrong, not the app.
        def walk(routes):  # type: ignore[no-untyped-def]
            for r in routes:
                inner = getattr(r, "original_router", None)
                if inner is not None:
                    yield from walk(inner.routes)
                    continue
                path = getattr(r, "path", None)
                if path:
                    yield path, set(getattr(r, "methods", set()) or set())

        seen = {p: m for p, m in walk(app.routes) if p in wanted}
        assert wanted <= set(seen), f"missing media routes: {wanted - set(seen)}"
        for path, methods in seen.items():
            assert {"GET", "HEAD"} <= methods, f"{path} answers {sorted(methods)}"
    finally:
        get_api_settings.cache_clear()
