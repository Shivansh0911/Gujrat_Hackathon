"""What "absent from the catalogue" means depends on where the camera got to.

The sync used to move every vanished camera to UNREACHABLE and swallow the
IllegalTransition when that was refused. For a DRAFT camera it is always refused -- and
that is the common case, not the rare one: when the estate renamed every camera, thirty
DRAFT rows for cameras that no longer existed stayed DRAFT indefinitely and the registry
went on claiming an estate twice its real size.

These assert the lifecycle facts the fix rests on, so a future change to the transition
table cannot quietly reintroduce the silent branch.
"""

from __future__ import annotations

import pytest

from services.registry.enums import CameraStatus, IllegalTransition, assert_transition


def test_a_working_camera_that_vanishes_becomes_unreachable():
    """It may come back, and the lifecycle allows exactly that."""
    assert_transition(CameraStatus.ACTIVE, CameraStatus.UNREACHABLE)
    assert_transition(CameraStatus.UNREACHABLE, CameraStatus.ACTIVE)


def test_a_draft_camera_cannot_become_unreachable():
    """The transition the sync used to attempt for every absent camera.

    "Unreachable" describes a camera that was working and stopped. A DRAFT camera was
    never onboarded, so the word does not apply to it -- and the lifecycle says so.
    """
    with pytest.raises(IllegalTransition):
        assert_transition(CameraStatus.DRAFT, CameraStatus.UNREACHABLE)


def test_a_draft_camera_that_vanishes_can_be_decommissioned():
    """The move the sync makes instead, which is legal and means the right thing."""
    assert_transition(CameraStatus.DRAFT, CameraStatus.DECOMMISSIONED)


def test_a_decommissioned_camera_is_not_revived_by_absence():
    """Absence must not re-retire or resurrect anything; the lifecycle wins."""
    with pytest.raises(IllegalTransition):
        assert_transition(CameraStatus.DECOMMISSIONED, CameraStatus.UNREACHABLE)
