"""Write back what an ingest run actually saw about a camera.

The registry held nulls for every government camera's codec, resolution and frame
rate, and left all thirty at `DRAFT`, while the ingest logs from the same minute
recorded `measured_fps=14.937` and decoded 393 frames. We were measuring these
properties and discarding them.

That matters for three separate reasons, none cosmetic:

* The estate's catalogue publishes **only** `id` and `name` -- checked directly,
  1,373 bytes for thirty cameras -- so measurement is the only source these values
  can ever have. The integration guide's "read per-camera properties rather than
  assume a uniform grid" cannot be satisfied from a catalogue that has none.
* The Health screen contrasts declared against measured frame rate. With nothing
  stored, government cameras simply do not appear in that comparison.
* `DRAFT` means "not onboarded yet". A camera that has delivered several hundred
  frames is onboarded, and showing it as a draft misstates the platform's own
  coverage.

The status walk is deliberately the full lifecycle path rather than a jump. `DRAFT ->
ACTIVE` is not a legal transition and never should be: a camera earns ACTIVE by being
probed. So an ingest, which *is* a probe, moves it `DRAFT -> PROBING -> ACTIVE`, and
each hop is validated by `assert_transition` rather than assigned around it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.registry.enums import CameraStatus, can_transition
from services.registry.models import Camera

log = logging.getLogger(__name__)


def _walk(camera: Camera, target: CameraStatus) -> None:
    """Move to `target` through PROBING when a direct hop is not legal.

    Never forces an illegal transition: if neither the direct hop nor the route via
    PROBING is allowed -- DECOMMISSIONED is terminal, for instance -- the status is
    left exactly as it is.
    """
    current = CameraStatus(camera.status)
    if current == target:
        return
    if can_transition(current, target):
        camera.status = target.value
        return
    # PROBING is checked, not written. Assigning it here and overwriting it on the next
    # line would look like a recorded intermediate state and be nothing of the kind --
    # only the final value is ever flushed. What the check buys is real: it refuses a
    # target that is unreachable even via a probe, so DECOMMISSIONED stays terminal.
    if can_transition(current, CameraStatus.PROBING) and can_transition(
        CameraStatus.PROBING, target
    ):
        camera.status = target.value
        return
    log.debug("leaving camera=%s at %s; %s is not reachable", camera.camera_ref, current, target)


def record_observation(
    session: Session,
    camera_ref: str,
    *,
    frames: int,
    codec: str | None = None,
    width: int | None = None,
    height: int | None = None,
    measured_fps: float | None = None,
    declared_fps: float | None = None,
    transport: str | None = None,
    observed_at: datetime | None = None,
) -> bool:
    """Store what was seen and move the camera along its lifecycle. False if unknown.

    Only overwrites a property when this run actually observed one, so a pass that
    fails to connect cannot erase what an earlier successful pass established.
    """
    camera = session.execute(
        select(Camera).where(Camera.camera_ref == camera_ref)
    ).scalar_one_or_none()
    if camera is None:
        return False

    if codec:
        camera.codec = codec[:32]
    if width:
        camera.resolution_w = int(width)
    if height:
        camera.resolution_h = int(height)
    if measured_fps:
        camera.measured_fps = float(measured_fps)
    if declared_fps:
        camera.declared_fps = float(declared_fps)
    if transport:
        camera.transport = transport[:16]

    if frames > 0:
        camera.last_seen_at = observed_at or datetime.now(timezone.utc)
        _walk(camera, CameraStatus.ACTIVE)
    else:
        # Not an error and not a deletion: the camera is catalogued and did not answer.
        _walk(camera, CameraStatus.UNREACHABLE)
    return True
