"""Write ANPR output to Postgres.

Two properties matter more than throughput here:

**Batched.** One round trip per detection across 30 cameras would spend the whole
budget on network latency rather than inference. Records accumulate and flush in
blocks.

**Idempotent.** `make demo` is run repeatedly, often minutes before a demonstration.
A second run of the same footage must not double every detection, because a journey
built from duplicated sightings shows a vehicle standing still at every camera. The
key is (camera, plate, pts_ms) -- the same plate at the same stream position in the
same camera is the same observation, however many times it is ingested.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, cast

from sqlalchemy import select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from services.analytics.anpr import PlateDetectionRecord
from services.registry.models import Camera

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50


@dataclass
class PersistStats:
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    unknown_camera: int = 0


class DetectionWriter:
    """Batching, idempotent writer for ANPR records."""

    def __init__(
        self, session: Session, *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> None:
        self._session = session
        self._batch_size = batch_size
        self._pending: list[PlateDetectionRecord] = []
        self._camera_ids: dict[str, uuid.UUID] = {}
        self.stats = PersistStats()

    def _camera_id(self, camera_ref: str) -> uuid.UUID | None:
        if camera_ref not in self._camera_ids:
            found = self._session.execute(
                select(Camera.id).where(Camera.camera_ref == camera_ref)
            ).scalar_one_or_none()
            if found is None:
                return None
            self._camera_ids[camera_ref] = found
        return self._camera_ids[camera_ref]

    def add(self, record: PlateDetectionRecord) -> None:
        self.stats.received += 1
        self._pending.append(record)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def extend(self, records: Iterable[PlateDetectionRecord]) -> None:
        for record in records:
            self.add(record)

    def flush(self) -> None:
        """Write the pending batch. Existing observations are skipped, not replaced."""
        if not self._pending:
            return

        rows = []
        for rec in self._pending:
            camera_id = self._camera_id(rec.camera_ref)
            if camera_id is None:
                # A detection with no registry row cannot be resolved to a place, so
                # it cannot contribute to a journey. Counted and dropped loudly
                # rather than written against a fabricated camera.
                self.stats.unknown_camera += 1
                log.warning("no registry camera for ref=%s; detection dropped", rec.camera_ref)
                continue
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "camera_id": camera_id,
                    "observed_at_utc": rec.observed_at_utc,
                    "plate_raw": rec.plate.raw[:64],
                    "plate_normalised": rec.plate.normalised[:32],
                    "corrections": [c.to_dict() for c in rec.plate.corrections],
                    "confidence": max(0.0, min(1.0, float(rec.plate.confidence))),
                    "pts_ms": float(rec.first_pts_ms),
                    "clock_confidence": float(rec.clock_confidence),
                    "crop_path": rec.crop_path,
                    "vehicle_bbox": (
                        {"x1": rec.bbox[0], "y1": rec.bbox[1], "x2": rec.bbox[2], "y2": rec.bbox[3]}
                        if rec.bbox
                        else None
                    ),
                }
            )

        self._pending.clear()
        if not rows:
            return

        # Idempotency is enforced by a NOT EXISTS guard rather than a unique index:
        # `detection` is a Timescale hypertable, and a unique constraint there must
        # include the partitioning column, which would make the natural key awkward.
        # The guard costs one indexed lookup per row and is exact.
        stmt = text(
            """
            INSERT INTO detection (
                id, camera_id, observed_at_utc, plate_raw, plate_normalised,
                corrections, confidence, pts_ms, ingested_at_utc, clock_confidence,
                crop_path, vehicle_bbox
            )
            SELECT CAST(:id AS uuid), CAST(:camera_id AS uuid),
                   CAST(:observed_at_utc AS timestamptz),
                   CAST(:plate_raw AS varchar(64)),
                   CAST(:plate_normalised AS varchar(32)),
                   CAST(:corrections AS jsonb),
                   CAST(:confidence AS real), CAST(:pts_ms AS double precision), now(),
                   CAST(:clock_confidence AS double precision),
                   CAST(:crop_path AS text), CAST(:vehicle_bbox AS jsonb)
            WHERE NOT EXISTS (
                SELECT 1 FROM detection d
                WHERE d.camera_id = CAST(:camera_id AS uuid)
                  AND d.plate_normalised = CAST(:plate_normalised AS varchar(32))
                  AND d.pts_ms = CAST(:pts_ms AS double precision)
            )
            """
        )

        import json

        for row in rows:
            payload = dict(row)
            payload["corrections"] = json.dumps(payload["corrections"])
            payload["vehicle_bbox"] = (
                json.dumps(payload["vehicle_bbox"]) if payload["vehicle_bbox"] else None
            )
            # `Session.execute` is typed as returning `Result`, which has no
            # rowcount; the concrete object for a DML statement is a CursorResult.
            result = cast(CursorResult[Any], self._session.execute(stmt, payload))
            if result.rowcount:
                self.stats.inserted += 1
            else:
                self.stats.duplicates += 1

        self._session.flush()

    def __enter__(self) -> "DetectionWriter":
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> None:
        if exc_type is None:
            self.flush()


def persist_records(
    session: Session,
    records: Iterator[PlateDetectionRecord],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> PersistStats:
    """Consume a pipeline's output into the database. Commits once at the end."""
    with DetectionWriter(session, batch_size=batch_size) as writer:
        writer.extend(records)
    session.commit()
    return writer.stats
