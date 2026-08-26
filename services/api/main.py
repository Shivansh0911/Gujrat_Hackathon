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
from services.api.routers import auth, cameras, system
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


app.include_router(auth.router)
app.include_router(cameras.router)
app.include_router(system.router)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "setu-api", "docs": "/docs", "openapi": "/openapi.json"}
