"""SETU API — FastAPI application.

OpenAPI 3.1 is the contract the console's TypeScript client is generated from. A
hand-maintained client is how a UI silently drifts from its API, so the schema is the
single source of truth in both directions.
"""

from __future__ import annotations

import logging

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.api.config import get_api_settings
from services.api import gateway_proxy
from services.api.routers import (
    alerts,
    analytics,
    auth,
    cameras,
    demo,
    gaps,
    journey,
    system,
    zones,
)
from services.common import redact

redact.install(level=logging.INFO)

settings = get_api_settings()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the gateway watch for the life of the process.

    Started here rather than on first request so that "unreachable since" is anchored
    to when the service came up, not to whenever somebody first happened to look.

    On a free-tier host that sleeps after idle, the watcher sleeps with it and the
    recorded history has a hole. That is a property of the hosting rather than a bug,
    and the console shows `last_checked_at` so the gap is visible instead of being
    mistaken for continuous observation.
    """
    from services.api import gateway_watch

    gateway_watch.start()
    try:
        yield
    finally:
        await gateway_watch.stop()


app = FastAPI(
    title="Project SETU",
    version="0.1.0",
    description=(
        "Unified CCTV registry, analytics and route reconstruction for the Gujarat "
        "Police Innovation Challenge 2026."
    ),
    openapi_version="3.1.0",
    lifespan=_lifespan,
)

# Explicit origin list, never "*". A wildcard origin with credentialed requests is
# rejected by browsers and is a standing prohibition in SECURITY.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request, call_next):  # type: ignore[no-untyped-def]
    """Baseline hardening headers on every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # The API serves JSON, never HTML, so the strictest CSP is also the correct one.
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


# Order matters. FastAPI matches routes in registration order, and
# /cameras/gap-analysis would otherwise be swallowed by /cameras/{camera_id} and
# rejected as a malformed UUID. The specific path must be registered first.
app.include_router(auth.router)
app.include_router(gaps.router)
app.include_router(zones.router)
app.include_router(cameras.router)
app.include_router(system.router)
app.include_router(alerts.router)
app.include_router(journey.router)
app.include_router(analytics.router)
app.include_router(demo.router)
app.include_router(gateway_proxy.router)


from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402


@app.get("/media/crops/{name}", include_in_schema=False)
def evidence_crop(name: str, exp: int = 0, sig: str = "") -> FileResponse:
    """Serve one evidence crop, by signed URL.

    Two separate protections, because they stop different things.

    **Traversal**: only the basename is accepted and it is resolved inside the crop
    directory, so `../../etc/passwd` cannot escape. The check is on the resolved path
    rather than on the string, because string filtering misses encodings.

    **Authorisation**: the URL must carry a signature this API issued (see
    `media_signing`). Without it the endpoint was open to anyone who could reach the
    host, and crop filenames are structured -- `camera_pts_plate.jpg` -- so they can
    be guessed rather than merely leaked. These are photographs of vehicles and their
    plates, assembled into an investigative record; they are not public files that
    happen to live behind an API.

    The signature is what lets an `<img>` tag work at all, since a browser cannot put
    an `Authorization` header on one. It is bound to a single filename and expires.
    """
    from pathlib import Path

    from services.api.config import get_api_settings
    from services.api.media_signing import verify_media_name
    from services.common.paths import CROPS_DIR

    settings = get_api_settings()
    if not verify_media_name(name, exp, sig, settings.jwt_secret):
        # 404, not 403: a distinct "exists but you may not have it" tells an
        # unauthenticated caller which filenames are real.
        raise HTTPException(status_code=404, detail="crop not found")

    crop_dir = CROPS_DIR.resolve()
    candidate = (crop_dir / Path(name).name).resolve()
    if crop_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="crop not found")
    return FileResponse(candidate, media_type="image/jpeg")


@app.get("/media/own-feed/{name}", include_in_schema=False)
def own_feed_clip(name: str, exp: int = 0, sig: str = "") -> FileResponse:
    """Serve the bundled own-feed clip for the replay cameras, by signed URL.

    The replay cameras are backed by a file rather than a live gateway stream, and
    `/cameras/{id}/stream-url` used to hand the console a `/media/own-feed/.../index.m3u8`
    URL that **nothing served** -- so previewing one of them showed a player error.
    The clip is a plain MP4, so it is served as one, with FileResponse handling range
    requests for seeking.
    """
    from pathlib import Path

    from services.api.config import get_api_settings
    from services.api.media_signing import verify_media_name
    from services.common.paths import OWN_FEED_DIR

    settings = get_api_settings()
    if not verify_media_name(name, exp, sig, settings.jwt_secret):
        raise HTTPException(status_code=404, detail="clip not found")

    feed_dir = OWN_FEED_DIR.resolve()
    candidate = (feed_dir / Path(name).name).resolve()
    if feed_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="clip not found")
    return FileResponse(candidate, media_type="video/mp4")


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "setu-api", "docs": "/docs", "openapi": "/openapi.json"}
