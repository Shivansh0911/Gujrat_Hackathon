"""Manual camera onboarding — the single-camera path beside the bulk one.

Model 1 asks for both to be demonstrable. These pin the rules that differ from bulk
import, which are the ones worth having a test for:

* a duplicate reference is refused rather than silently overwriting, because a
  registry that quietly merges two cameras files one camera's evidence under another's
* coordinates are all-or-nothing, since half a position places a camera on the prime
  meridian
* a supplied position must state its uncertainty, or it reads as survey-grade
* the camera starts DRAFT, never ACTIVE — nothing has been probed yet

The endpoint half needs Postgres for the audit ledger and the CHECK constraint on
`geom`, and skips without it, the same way the rest of this suite does.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text


def _db_url() -> str | None:
    if os.environ.get("SETU_DATABASE_URL"):
        return os.environ["SETU_DATABASE_URL"]
    from services.common.paths import ENV_FILE

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("SETU_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


_URL = _db_url()
endpoint = pytest.mark.skipif(
    not _reachable(_URL), reason="Postgres not reachable; camera-create tests need it"
)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from services.api.main import app

    return TestClient(app)


def _token(client, role: str) -> str | None:
    from services.api.config import get_api_settings

    settings = get_api_settings()
    pw = settings.admin_password if role == "admin" else settings.operator_password
    if not pw:
        return None
    r = client.post("/auth/login", data={"username": role, "password": pw})
    return str(r.json()["access_token"]) if r.status_code == 200 else None


def _body(**over):
    body = {
        "camera_ref": f"MANUAL-{uuid.uuid4().hex[:8]}",
        "name": "Manually onboarded camera",
        "location_text": "Bhavani Char Rasta, Ahmedabad",
    }
    body.update(over)
    return body


# ------------------------------------------------------------------ schema rules


def test_the_schema_rejects_an_out_of_range_coordinate() -> None:
    """Pure validation, no database needed."""
    import pydantic

    from services.api.schemas import CameraCreate

    with pytest.raises(pydantic.ValidationError):
        CameraCreate(camera_ref="X", name="X", lat=91.0, lon=0.0)
    with pytest.raises(pydantic.ValidationError):
        CameraCreate(camera_ref="X", name="X", lat=0.0, lon=181.0)


def test_a_camera_with_no_coordinate_is_valid() -> None:
    """A camera nobody has placed yet is a real state, not an error.

    Two of the thirty in this estate are in it. Forcing a number here would be
    inventing a position, which is the one thing the registry must never do.
    """
    from services.api.schemas import CameraCreate

    c = CameraCreate(camera_ref="X", name="X")
    assert c.lat is None and c.lon is None


# --------------------------------------------------------------------- endpoint


@endpoint
def test_a_camera_can_be_created_without_a_coordinate(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.post("/cameras", headers={"Authorization": f"Bearer {token}"}, json=_body())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["coordinate_missing"] is True
    assert out["geom_source"] == "unset"
    # DRAFT, not ACTIVE: nothing has connected to this camera yet.
    assert out["status"] == "DRAFT"
    assert out["detection_count"] == 0


@endpoint
def test_a_camera_with_a_coordinate_is_marked_manual_survey(client) -> None:
    """A person supplied the position, and the map renders that provenance."""
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.post(
        "/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(lat=23.0225, lon=72.5714, confidence_radius_m=25),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["geom_source"] == "manual_survey"
    assert out["coordinate_missing"] is False
    assert abs(out["lat"] - 23.0225) < 1e-6


@endpoint
def test_a_position_without_an_uncertainty_is_refused(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.post(
        "/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(lat=23.0225, lon=72.5714),
    )
    assert r.status_code == 422
    assert "confidence_radius_m" in r.json()["detail"]


@endpoint
def test_half_a_coordinate_is_refused(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.post(
        "/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json=_body(lat=23.0225, confidence_radius_m=25),
    )
    assert r.status_code == 422
    assert "both" in r.json()["detail"]


@endpoint
def test_a_duplicate_reference_is_refused_not_merged(client) -> None:
    """Silently updating would file one camera's evidence under another's."""
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")
    headers = {"Authorization": f"Bearer {token}"}

    body = _body()
    first = client.post("/cameras", headers=headers, json=body)
    assert first.status_code == 201

    second = client.post("/cameras", headers=headers, json=body)
    assert second.status_code == 409
    assert body["camera_ref"] in second.json()["detail"]


@endpoint
def test_an_operator_may_not_onboard_a_camera(client) -> None:
    """Onboarding asserts that surveillance exists somewhere. That is an admin act."""
    token = _token(client, "operator")
    if token is None:
        pytest.skip("no operator credentials configured")

    r = client.post("/cameras", headers={"Authorization": f"Bearer {token}"}, json=_body())
    assert r.status_code == 403


@endpoint
def test_creation_is_written_to_the_audit_ledger(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    engine = create_engine(_URL, future=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        before = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'CAMERA_CREATED'")
        ).scalar_one()

    body = _body()
    r = client.post("/cameras", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert r.status_code == 201, r.text

    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        after = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'CAMERA_CREATED'")
        ).scalar_one()
        latest = (
            conn.execute(
                text(
                    "SELECT actor_role, detail FROM audit_entry"
                    " WHERE action = 'CAMERA_CREATED' ORDER BY seq DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert after == before + 1
    assert latest is not None
    assert latest["actor_role"] == "admin"
    assert latest["detail"]["camera_ref"] == body["camera_ref"]
