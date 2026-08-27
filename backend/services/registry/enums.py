"""Domain enumerations and the camera lifecycle state machine.

The lifecycle is enforced here rather than by convention, because "which states may
follow which" is exactly the kind of rule that decays into `camera.status = whatever`
scattered across route handlers. A single transition table, checked on every write, is
what makes the HLD's lifecycle claim true of the running system.
"""

from __future__ import annotations

from enum import StrEnum


class CameraStatus(StrEnum):
    """Lifecycle of a camera in the registry.

    DRAFT             onboarded, nothing verified yet
    PROBING           being probed for capabilities and reachability
    PENDING_CONSENT   private camera awaiting a valid consent artefact
    ACTIVE            reachable, authorised, analytics permitted
    DEGRADED          reachable but impaired (fps drift, decode errors, reconnects)
    UNREACHABLE       no longer servable, or vanished from its source catalogue
    CONSENT_REVOKED   consent expired or withdrawn; access denied
    DECOMMISSIONED    retired. Row is retained: evidence references cameras.
    """

    DRAFT = "DRAFT"
    PROBING = "PROBING"
    PENDING_CONSENT = "PENDING_CONSENT"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    DECOMMISSIONED = "DECOMMISSIONED"


# Legal transitions. Anything absent here is rejected.
#
# Two rules worth stating explicitly because they are load-bearing:
#   * DECOMMISSIONED is terminal. There is no path out. Evidence rows reference
#     cameras, so a decommissioned camera must never be silently revived into a
#     different physical device under the same identity.
#   * UNREACHABLE -> ACTIVE is permitted. The gateway's media plane failed
#     independently of its control plane on 2026-08-25 (DISCOVERY finding 9); a camera
#     that comes back must recover without operator intervention.
_TRANSITIONS: dict[CameraStatus, frozenset[CameraStatus]] = {
    CameraStatus.DRAFT: frozenset(
        {CameraStatus.PROBING, CameraStatus.PENDING_CONSENT, CameraStatus.DECOMMISSIONED}
    ),
    CameraStatus.PROBING: frozenset(
        {
            CameraStatus.ACTIVE,
            CameraStatus.PENDING_CONSENT,
            CameraStatus.UNREACHABLE,
            CameraStatus.DEGRADED,
            CameraStatus.DECOMMISSIONED,
        }
    ),
    CameraStatus.PENDING_CONSENT: frozenset(
        {CameraStatus.ACTIVE, CameraStatus.CONSENT_REVOKED, CameraStatus.DECOMMISSIONED}
    ),
    CameraStatus.ACTIVE: frozenset(
        {
            CameraStatus.DEGRADED,
            CameraStatus.UNREACHABLE,
            CameraStatus.CONSENT_REVOKED,
            CameraStatus.PROBING,
            CameraStatus.DECOMMISSIONED,
        }
    ),
    CameraStatus.DEGRADED: frozenset(
        {
            CameraStatus.ACTIVE,
            CameraStatus.UNREACHABLE,
            CameraStatus.CONSENT_REVOKED,
            CameraStatus.PROBING,
            CameraStatus.DECOMMISSIONED,
        }
    ),
    CameraStatus.UNREACHABLE: frozenset(
        {
            CameraStatus.ACTIVE,
            CameraStatus.DEGRADED,
            CameraStatus.PROBING,
            CameraStatus.CONSENT_REVOKED,
            CameraStatus.DECOMMISSIONED,
        }
    ),
    CameraStatus.CONSENT_REVOKED: frozenset(
        # Consent can be renewed, which returns the camera to the pending gate --
        # never straight to ACTIVE, so a renewal is always re-verified.
        {CameraStatus.PENDING_CONSENT, CameraStatus.DECOMMISSIONED}
    ),
    CameraStatus.DECOMMISSIONED: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a status change is not permitted by the lifecycle."""

    def __init__(self, current: CameraStatus, requested: CameraStatus) -> None:
        allowed = sorted(t.value for t in _TRANSITIONS[current])
        super().__init__(
            f"cannot move camera from {current.value} to {requested.value}; "
            f"allowed from {current.value}: {allowed or ['(terminal)']}"
        )
        self.current = current
        self.requested = requested


def can_transition(current: CameraStatus, requested: CameraStatus) -> bool:
    return requested in _TRANSITIONS[current]


def assert_transition(current: CameraStatus, requested: CameraStatus) -> None:
    """Raise IllegalTransition unless the move is legal. Idempotent moves allowed."""
    if current == requested:
        # A no-op write (e.g. a health probe reconfirming ACTIVE) must not raise;
        # otherwise every caller needs a guard and some will forget it.
        return
    if not can_transition(current, requested):
        raise IllegalTransition(current, requested)


class GeomSource(StrEnum):
    """Provenance of a camera's coordinate.

    UNSET is not "missing data we will fill in later" -- it is a positive statement
    that we do not know where this camera is, and it propagates: unset cameras are
    excluded from spatial queries and surfaced as `coordinate missing` rather than
    dropped. A camera with an invented position is worse than one with none, because
    it produces an authoritative-looking route that is wrong.
    """

    UNSET = "unset"
    APPROXIMATE = "approximate"
    MANUAL_SURVEY = "manual_survey"


class SourceType(StrEnum):
    """Which CameraSource implementation serves this camera."""

    GATEWAY = "gateway"  # the organiser's hackathon gateway
    FILE = "file"  # local footage, our own-feed demonstration
    RTSP_GENERIC = "rtsp"  # a direct RTSP/ONVIF camera or departmental VMS
    ONVIF = "onvif"


class ArchiveMode(StrEnum):
    """Where this camera's video is retained.

    Unused in Tier 0 and present deliberately: the HLD claims a three-step migration
    to Model 4's central archive, and that claim is only true if the flag the
    migration pivots on exists from the first migration rather than being added by a
    table rewrite later.
    """

    DEPARTMENTAL = "departmental"  # video stays with the owning department
    FEDERATED = "federated"  # pulled into the central archive


class OwnershipClass(StrEnum):
    GOVERNMENT = "GOVERNMENT"
    PRIVATE_PUBLIC_FACING = "PRIVATE_PUBLIC_FACING"


class EvidenceType(StrEnum):
    """How a journey hop was established. Ordered by evidentiary strength."""

    ANPR_EXACT = "anpr_exact"
    ANPR_FUZZY = "anpr_fuzzy"
    APPEARANCE_REID = "appearance_reid"  # T2.3; always a lower grade than a plate read


class AlertState(StrEnum):
    RAISED = "RAISED"
    NOTIFIED = "NOTIFIED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"


class AlertDisposition(StrEnum):
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"
    UNABLE_TO_VERIFY = "unable_to_verify"


class Role(StrEnum):
    OPERATOR = "operator"
    ADMIN = "admin"
