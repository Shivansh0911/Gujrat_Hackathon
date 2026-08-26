"""SQLAlchemy models — the Model 1 control plane.

Two conventions run through every table here and are worth stating once:

**Declared vs measured.** Wherever the platform records something a source *claims*
(`declared_fps`, upstream `live`), the measured counterpart sits beside it
(`measured_fps`, `status`). This is not redundancy: DISCOVERY findings 2 and 9 showed
the gateway's catalogue reporting `fps: 0.0` for 20 of 30 cameras and flagging all 30
`live: true` while every playlist returned 502. Keeping both lets the platform show a
jury the discrepancy instead of quietly trusting one of them.

**No hard deletes.** Detections and evidence reference cameras. A camera that leaves
its source catalogue becomes UNREACHABLE; a retired one becomes DECOMMISSIONED. Rows
persist so that historical evidence never dangles.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from services.registry.enums import (
    AlertState,
    ArchiveMode,
    CameraStatus,
    GeomSource,
    OwnershipClass,
    SourceType,
)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _utcnow() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Department(Base):
    """An owning government department. The tenant boundary for RLS (T1.5)."""

    __tablename__ = "department"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _utcnow()

    sites: Mapped[list[Site]] = relationship(back_populates="department")
    cameras: Mapped[list[Camera]] = relationship(back_populates="department")


class Site(Base):
    """A physical location grouping cameras, e.g. a bus port or a check post."""

    __tablename__ = "site"
    __table_args__ = (
        UniqueConstraint("department_id", "name", name="uq_site_dept_name"),
        Index("ix_site_geom", "geom", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    department_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("department.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    district: Mapped[str | None] = mapped_column(String(120), index=True)
    # Nullable for the same reason camera.geom is: an unknown location is recorded as
    # unknown, never as a plausible-looking guess.
    geom: Mapped[Any | None] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    created_at: Mapped[datetime] = _utcnow()

    department: Mapped[Department] = relationship(back_populates="sites")
    cameras: Mapped[list[Camera]] = relationship(back_populates="site")


class Camera(Base):
    """One camera. The registry row every other layer resolves a camera through."""

    __tablename__ = "camera"
    __table_args__ = (
        UniqueConstraint("source_type", "camera_ref", name="uq_camera_source_ref"),
        # A coordinate must be present exactly when its provenance says it is. Without
        # this, an 'unset' row can acquire a geometry through an unrelated code path
        # and silently start appearing in spatial results.
        CheckConstraint(
            "(geom_source = 'unset' AND geom IS NULL) OR "
            "(geom_source <> 'unset' AND geom IS NOT NULL)",
            name="ck_camera_geom_matches_source",
        ),
        CheckConstraint(
            "confidence_radius_m IS NULL OR confidence_radius_m > 0",
            name="ck_camera_radius_positive",
        ),
        Index("ix_camera_geom", "geom", postgresql_using="gist"),
        Index("ix_camera_dept_status", "department_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Identifier as the *source* knows it. Gateway ids can change (§2.1), so this is
    # scoped by source_type rather than assumed globally unique.
    camera_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    location_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    department_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("department.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("site.id", ondelete="RESTRICT"), index=True
    )

    # --- Geometry and its provenance (see GeomSource docstring) ---
    geom: Mapped[Any | None] = mapped_column(Geography("POINT", srid=4326, spatial_index=False))
    geom_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=GeomSource.UNSET.value
    )
    confidence_radius_m: Mapped[float | None] = mapped_column(Float)
    resolved_by: Mapped[str | None] = mapped_column(String(200))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heading_deg: Mapped[int | None] = mapped_column(Integer)

    # --- Lifecycle ---
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CameraStatus.DRAFT.value, index=True
    )
    ownership_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OwnershipClass.GOVERNMENT.value
    )

    # --- Stream properties: declared by the source, measured by us ---
    codec: Mapped[str | None] = mapped_column(String(32))
    resolution_w: Mapped[int | None] = mapped_column(Integer)
    resolution_h: Mapped[int | None] = mapped_column(Integer)
    declared_fps: Mapped[float | None] = mapped_column(Float)   # reference only
    measured_fps: Mapped[float | None] = mapped_column(Float)   # from PTS deltas
    transport: Mapped[str | None] = mapped_column(String(16))   # rtsp | hls | file

    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SourceType.GATEWAY.value
    )
    # Never a credentialed URL. Credentials live in the secret store and never leave
    # the adapter process (§6); this column holds the reference only.
    source_uri: Mapped[str | None] = mapped_column(Text)

    archive_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ArchiveMode.DEPARTMENTAL.value
    )

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _utcnow()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    department: Mapped[Department] = relationship(back_populates="cameras")
    site: Mapped[Site | None] = relationship(back_populates="cameras")
    capabilities: Mapped[list[CameraCapability]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )

    @property
    def has_geom(self) -> bool:
        return self.geom_source != GeomSource.UNSET.value and self.geom is not None


class CameraCapability(Base):
    """A probed capability. Declared capabilities are verified before an adapter loads."""

    __tablename__ = "camera_capability"
    __table_args__ = (
        UniqueConstraint("camera_id", "name", name="uq_capability_camera_name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Whether a probe confirmed the claim, as opposed to the source asserting it.
    verified: Mapped[bool] = mapped_column(nullable=False, default=False)
    probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    camera: Mapped[Camera] = relationship(back_populates="capabilities")


class Detection(Base):
    """One plate read. Hypertable on observed_at_utc.

    Composite primary key because TimescaleDB requires the partitioning column to
    participate in every unique constraint.
    """

    __tablename__ = "detection"
    __table_args__ = (
        Index("ix_detection_plate_time", "plate_normalised", "observed_at_utc"),
        Index("ix_detection_camera_time", "camera_id", "observed_at_utc"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_detection_conf"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4, primary_key=True)
    # Part of the PK so Timescale can partition on it.
    observed_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, primary_key=True
    )

    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera.id", ondelete="RESTRICT"), nullable=False
    )

    plate_raw: Mapped[str] = mapped_column(String(64), nullable=False)
    plate_normalised: Mapped[str] = mapped_column(String(32), nullable=False)
    # Every character correction, with position, raw value, corrected value and
    # confidence. A corrected plate is never presented as clean.
    corrections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Stream position, retained so a detection can be re-derived from the original
    # stream rather than merely displayed. This is what makes output reproducible.
    pts_ms: Mapped[float] = mapped_column(Float, nullable=False)
    ingested_at_utc: Mapped[datetime] = _utcnow()
    # How much to trust observed_at_utc: a file source has an exact timeline, a live
    # stream's mapping to wall clock is an estimate.
    clock_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    crop_path: Mapped[str | None] = mapped_column(Text)
    vehicle_bbox: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class WatchlistEntry(Base):
    """A vehicle or person of interest. Sources include our representative list."""

    __tablename__ = "watchlist_entry"
    __table_args__ = (
        Index("ix_watchlist_plate", "plate_normalised"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    plate_normalised: Mapped[str | None] = mapped_column(String(32), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="vehicle")
    watchlist_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    case_ref: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)

    # Vehicle attributes, used to corroborate a plate match. A fuzzy match that also
    # agrees on colour is a materially stronger claim than the plate alone.
    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    colour: Mapped[str | None] = mapped_column(String(32))
    authority: Mapped[str | None] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    valid_from: Mapped[datetime] = _utcnow()
    # NOT NULL deliberately: an entry without an expiry becomes a permanent shadow
    # record on a citizen, outliving the investigation that justified it.
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alert(Base):
    """A watchlist match. Lifecycle deepens at T1.3."""

    __tablename__ = "alert"
    __table_args__ = (
        Index("ix_alert_state_time", "state", "raised_at"),
        # Deduplication key: same plate, same camera, same window collapses into one
        # alert carrying an observation count rather than N alerts.
        Index("ix_alert_dedup", "camera_id", "matched_value", "dedup_window_start"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    watchlist_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("watchlist_entry.id", ondelete="SET NULL")
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("camera.id", ondelete="RESTRICT"), nullable=False
    )
    detection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    matched_value: Mapped[str] = mapped_column(String(32), nullable=False)
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    observed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raised_at: Mapped[datetime] = _utcnow()
    dedup_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AlertState.RAISED.value
    )
    disposition: Mapped[str | None] = mapped_column(String(32))
    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Measured decode-to-alert latency. Recorded per alert so the HLD's "under 2s"
    # claim is evidenced by the system itself rather than by a one-off benchmark.
    latency_ms: Mapped[float | None] = mapped_column(Float)

    # Ordered observations behind a movement alert. Successive sightings of one
    # vehicle are one developing event, not a stream of near-identical alerts an
    # operator learns to dismiss.
    sightings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    is_movement: Mapped[bool] = mapped_column(nullable=False, default=False)
    # What agreed beyond the plate (colour, body type), and what did not.
    corroboration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )


class AuditEntry(Base):
    """Tamper-evident audit ledger.

    entry_hash = SHA256(prev_hash || canonical_json(entry)). `seq` is a plain
    autoincrement so the chain has a total order independent of clock skew.
    """

    __tablename__ = "audit_entry"
    __table_args__ = (
        Index("ix_audit_occurred", "occurred_at"),
        Index("ix_audit_subject", "subject_type", "subject_id"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = _utcnow()
    actor_id: Mapped[str | None] = mapped_column(String(200))
    actor_role: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # Mandatory for evidence export and journey queries; written BEFORE the query runs.
    purpose: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    prev_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    entry_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
