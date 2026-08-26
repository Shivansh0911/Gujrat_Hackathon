"""Request and response models. These generate the OpenAPI schema the console types from."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in_s: int


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=256)


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str


class CameraOut(BaseModel):
    """A camera as the console sees it.

    `coordinate_missing` is surfaced explicitly rather than left as a null the UI has
    to interpret. A camera we cannot place is a fact an operator must see -- it is the
    difference between "the vehicle was not there" and "we cannot say".
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    camera_ref: str
    name: str
    location_text: str
    department_id: uuid.UUID
    department_code: str | None = None

    lat: float | None = None
    lon: float | None = None
    geom_source: str
    confidence_radius_m: float | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    coordinate_missing: bool = False

    status: str
    codec: str | None = None
    resolution_w: int | None = None
    resolution_h: int | None = None
    declared_fps: float | None = None
    measured_fps: float | None = None
    transport: str | None = None
    source_type: str
    last_seen_at: datetime | None = None


class GeomPatch(BaseModel):
    """Operator pin-drop. Writes manual_survey provenance and an audit entry."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # An operator placing a pin on a map knows the site; 25 m reflects map precision
    # rather than survey equipment, and stays honest about it.
    confidence_radius_m: float = Field(default=25.0, gt=0, le=100_000)
    note: str | None = Field(default=None, max_length=500)


class CameraHealthOut(BaseModel):
    camera_id: uuid.UUID
    camera_ref: str
    name: str
    status: str
    transport: str | None = None
    declared_fps: float | None = None
    measured_fps: float | None = None
    fps_drift_pct: float | None = None
    last_seen_at: datetime | None = None
    coordinate_missing: bool = False


class SyncResult(BaseModel):
    """Outcome of a catalogue diff. Counts, plus the ids behind each so it is checkable."""

    catalogue_reachable: bool
    cameras_in_catalogue: int
    added: list[str] = []
    removed: list[str] = []
    properties_changed: list[str] = []
    unchanged: int = 0
    note: str | None = None


class AuditVerifyOut(BaseModel):
    valid: bool
    entries_checked: int
    breaks: list[dict[str, Any]]
    head_hash: str | None
    verified_at: str


class StreamUrlOut(BaseModel):
    """Playback URL for the console.

    The browser never receives an upstream credential: it asks for a camera by id and
    the platform returns only what that actor is permitted to play.
    """

    camera_id: uuid.UUID
    camera_ref: str
    transport: str
    url: str
    expires_in_s: int | None = None
