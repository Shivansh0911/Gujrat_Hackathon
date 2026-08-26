"""SETU API — FastAPI application.

OpenAPI 3.1 is the contract the console's TypeScript client is generated from. A
hand-maintained client is how a UI silently drifts from its API, so the schema is the
single source of truth in both directions.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.api.config import get_api_settings
from services.api.routers import alerts, auth, cameras, gaps, journey, system
from services.common import redact

redact.install(level=logging.INFO)

settings = get_api_settings()

app = FastAPI(
    title="Project SETU",
    version="0.1.0",
    description=(
        "Unified CCTV registry, analytics and route reconstruction for the Gujarat "
        "Police Innovation Challenge 2026."
    ),
    openapi_version="3.1.0",
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
app.include_router(cameras.router)
app.include_router(system.router)
app.include_router(alerts.router)
app.include_router(journey.router)


from fastapi import HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402


@app.get("/media/crops/{name}", include_in_schema=False)
def evidence_crop(name: str) -> FileResponse:
    """Serve one evidence crop by filename.

    Only the basename is accepted and it is resolved inside the crop directory, so a
    traversal attempt (../../etc/passwd) cannot escape. The check is on the resolved
    path rather than on the string, because string filtering misses encodings.
    """
    from pathlib import Path

    from services.common.paths import CROPS_DIR

    crop_dir = CROPS_DIR.resolve()
    candidate = (crop_dir / Path(name).name).resolve()
    if crop_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="crop not found")
    return FileResponse(candidate, media_type="image/jpeg")


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "setu-api", "docs": "/docs", "openapi": "/openapi.json"}
