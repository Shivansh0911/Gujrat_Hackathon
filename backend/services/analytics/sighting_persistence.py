"""Write vehicle sightings to Postgres.

Mirrors `persistence.DetectionWriter` -- batched and idempotent, for the same two
reasons -- with one difference that matters.

The idempotency key
-------------------
`DetectionWriter` keys on (camera, plate, pts_ms): the same plate at the same stream
position on the same camera is the same observation. A sighting has no plate, so the
natural key is **(camera, track_id, first_pts_ms)**. `track_id` is already namespaced by
session in `SightingPipeline._finalise`, so it survives the wrap of a looping recording
without colliding, and `first_pts_ms` pins the pass to a stream position.

That key is exact for re-ingesting the same footage, which is what `make demo` does
repeatedly and often minutes before a demonstration. It is *not* exact across a change
to the tracker's gates, which can split or merge passes and so renumber tracks -- a
re-ingest after retuning will write new rows rather than updating old ones. That is the
honest behaviour: the two runs genuinely disagree about what happened, and silently
overwriting one with the other would hide the disagreement.

Why the vector is bound as text
-------------------------------
pgvector's wire format for a parameter is its text form, `[0.1,0.2,...]`, cast on the
server. Binding a Python list through SQLAlchemy's generic adapter produces an array
literal instead and fails on type mismatch, which reads as a schema fault rather than a
binding one.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from services.analytics.sighting import VehicleSightingRecord
from services.registry.models import Camera

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50


@dataclass
class SightingPersistStats:
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    unknown_camera: int = 0
    #: Rows written with `appearance_vector IS NULL` because the crop failed the quality
    #: gate. They remain useful for counting, flow and dwell; they are simply not
    #: searchable by appearance. Counted so the ratio is visible rather than inferred.
    without_fingerprint: int = 0


_INSERT = text(
    """
    INSERT INTO vehicle_sighting (
        id, camera_id, observed_at_utc, vehicle_bbox, vehicle_class,
        vehicle_class_confidence, track_id, plate_detection_id, plate_detection_at,
        appearance_vector, appearance_kind, dominant_colour_lab,
        aspect_ratio, rel_area, rel_cx, rel_cy, quality_score,
        crop_path, pts_ms, clock_confidence, ingested_at_utc
    )
    SELECT CAST(:id AS uuid), CAST(:camera_id AS uuid),
           CAST(:observed_at_utc AS timestamptz),
           CAST(:vehicle_bbox AS jsonb),
           CAST(:vehicle_class AS varchar(32)),
           CAST(:class_confidence AS double precision),
           CAST(:track_id AS varchar(64)),
           NULL, NULL,
           CAST(:appearance_vector AS vector),
           CAST(:appearance_kind AS varchar(64)),
           CAST(:dominant_colour_lab AS jsonb),
           CAST(:aspect_ratio AS double precision),
           CAST(:rel_area AS double precision),
           CAST(:rel_cx AS double precision),
           CAST(:rel_cy AS double precision),
           CAST(:quality_score AS double precision),
           CAST(:crop_path AS text),
           CAST(:pts_ms AS double precision),
           CAST(:clock_confidence AS double precision),
           now()
    WHERE NOT EXISTS (
        SELECT 1 FROM vehicle_sighting s
        WHERE s.camera_id = CAST(:camera_id AS uuid)
          AND s.track_id  = CAST(:track_id AS varchar(64))
          AND s.pts_ms    = CAST(:pts_ms AS double precision)
    )
    """
)


class SightingWriter:
    """Batching, idempotent writer for vehicle sightings."""

    def __init__(self, session: Session, *, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self._session = session
        self._batch_size = batch_size
        self._pending: list[VehicleSightingRecord] = []
        self._camera_ids: dict[str, uuid.UUID] = {}
        self.stats = SightingPersistStats()

    def _camera_id(self, camera_ref: str) -> uuid.UUID | None:
        if camera_ref not in self._camera_ids:
            found = self._session.execute(
                select(Camera.id).where(Camera.camera_ref == camera_ref)
            ).scalar_one_or_none()
            if found is None:
                return None
            self._camera_ids[camera_ref] = found
        return self._camera_ids[camera_ref]

    def add(self, record: VehicleSightingRecord) -> None:
        self.stats.received += 1
        self._pending.append(record)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def extend(self, records: Iterable[VehicleSightingRecord]) -> None:
        for record in records:
            self.add(record)

    def flush(self) -> None:
        if not self._pending:
            return

        pending, self._pending = self._pending, []
        for rec in pending:
            camera_id = self._camera_id(rec.camera_ref)
            if camera_id is None:
                # A sighting with no registry row cannot be resolved to a place, so it
                # cannot contribute to a journey. Counted and dropped loudly rather than
                # written against a fabricated camera.
                self.stats.unknown_camera += 1
                log.warning("no registry camera for ref=%s; sighting dropped", rec.camera_ref)
                continue

            appearance = rec.appearance
            if appearance is None:
                self.stats.without_fingerprint += 1

            x1, y1, x2, y2 = rec.bbox
            params: dict[str, Any] = {
                "id": uuid.uuid4(),
                "camera_id": camera_id,
                "observed_at_utc": rec.observed_at_utc,
                "vehicle_bbox": json.dumps({"x1": x1, "y1": y1, "x2": x2, "y2": y2}),
                "vehicle_class": rec.vehicle_class.value,
                "class_confidence": max(0.0, min(1.0, float(rec.class_confidence))),
                "track_id": rec.track_id[:64],
                # pgvector takes its text form; see the module docstring.
                "appearance_vector": (
                    "[" + ",".join(f"{v:.6g}" for v in appearance.vector) + "]"
                    if appearance
                    else None
                ),
                "appearance_kind": appearance.kind if appearance else None,
                "dominant_colour_lab": (
                    json.dumps(appearance.dominant_colour_lab) if appearance else None
                ),
                "aspect_ratio": appearance.aspect_ratio if appearance else _aspect(rec.bbox),
                "rel_area": rec.rel_area,
                "rel_cx": rec.rel_cx,
                "rel_cy": rec.rel_cy,
                "quality_score": appearance.quality_score if appearance else 0.0,
                "crop_path": rec.crop_path,
                "pts_ms": float(rec.first_pts_ms),
                "clock_confidence": max(0.0, min(1.0, float(rec.clock_confidence))),
            }
            result = cast(CursorResult[Any], self._session.execute(_INSERT, params))
            if result.rowcount:
                self.stats.inserted += 1
            else:
                self.stats.duplicates += 1

        self._session.flush()

    def __enter__(self) -> "SightingWriter":
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> None:
        if exc_type is None:
            self.flush()


def _aspect(bbox: tuple[int, int, int, int]) -> float:
    """Aspect ratio from the box, for a sighting with no fingerprint to carry one."""
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return round(w / h, 4) if h > 0 else 0.0


def persist_sightings(
    session: Session,
    records: Iterator[VehicleSightingRecord],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SightingPersistStats:
    """Consume a pipeline's output into the database. Commits once at the end."""
    with SightingWriter(session, batch_size=batch_size) as writer:
        writer.extend(records)
    session.commit()
    return writer.stats


__all__ = [
    "SightingPersistStats",
    "SightingWriter",
    "persist_sightings",
]
