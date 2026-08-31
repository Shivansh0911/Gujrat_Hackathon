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

    #: How many detections this camera actually has behind it.
    #:
    #: A pin with no detections and a pin with two hundred look identical on a map,
    #: and that ambiguity is what made a reviewer read thirty registry positions from
    #: the government catalogue as thirty working cameras. The console greys out the
    #: empty ones, which it can only do if the count comes with the camera.
    detection_count: int = 0


class GeomPatch(BaseModel):
    """Operator pin-drop. Writes manual_survey provenance and an audit entry."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # An operator placing a pin on a map knows the site; 25 m reflects map precision
    # rather than survey equipment, and stays honest about it.
    confidence_radius_m: float = Field(default=25.0, gt=0, le=100_000)
    note: str | None = Field(default=None, max_length=500)


class CameraCreate(BaseModel):
    """One camera, onboarded by hand.

    Bulk import exists for a departmental spreadsheet; this is for the single camera
    an officer is standing in front of. Both paths validate coordinates the same way
    and both write to the audit ledger, because onboarding a camera is an assertion
    that surveillance exists at a place and should be attributable either way.

    Coordinates are optional, deliberately. A camera whose position nobody has
    established yet is a real and common state -- two of the thirty in this estate are
    in it -- and the registry records that honestly as `geom_source=unset` rather than
    forcing an invented number. It can be placed later with the map's pin-drop.
    """

    camera_ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    location_text: str = Field(default="", max_length=500)
    department_code: str | None = Field(default=None, max_length=32)

    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    #: Metres. Required when coordinates are supplied: a position with no stated
    #: uncertainty reads as survey-grade, and an operator typing a rooftop location
    #: off a map is not that.
    confidence_radius_m: float | None = Field(default=None, gt=0, le=100_000)
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


class BulkImportRejection(BaseModel):
    """One row that did not land, and why. Line numbers count the header as line 1."""

    line: int
    camera_ref: str | None = None
    reasons: list[str]


class BulkImportResult(BaseModel):
    """Outcome of a bulk camera onboarding.

    Partial success is the normal case and is reported as such: an operator importing
    a departmental spreadsheet wants the good rows in and a list of the ones to fix,
    not an all-or-nothing rejection with no indication of which line is wrong.
    """

    rows_read: int
    accepted: int
    rejected: int
    created: int
    updated: int
    unset_coordinates: int
    rejections: list[BulkImportRejection] = []
    note: str | None = None


class GatewayStatusOut(BaseModel):
    """Whether the government gateway is answering, and since when.

    `reachable` is deliberately three-valued. `null` means the watcher has not
    completed a check yet -- on a cold start, or on a free-tier host that was asleep --
    and the console says "not yet checked" rather than presenting an unknown as an
    outage. Reporting a state we have not observed is how a dashboard becomes
    something nobody trusts.
    """

    reachable: bool | None = None
    last_checked_at: str | None = None
    last_success_at: str | None = None
    unreachable_since: str | None = None
    cameras_in_catalogue: int | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    checks_performed: int = 0
    poll_interval_s: float = 60.0


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
