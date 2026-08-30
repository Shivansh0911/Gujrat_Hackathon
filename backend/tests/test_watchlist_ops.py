"""Watchlist plate validation, and removal that leaves a trail.

The validation half is pure and runs everywhere. The endpoint half needs Postgres --
the audit ledger, RLS and the append-only trigger are enforced in the database, not in
Python -- and skips without one, the same way `test_row_level_security.py` does.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from services.analytics.plate_grammar import normalise_plate

# ------------------------------------------------------- the shared grammar


@pytest.mark.parametrize(
    "plate",
    [
        "GJ01AB1234",  # the standard form
        "KA25AB1542",  # the plate the demo journey traces
        "GJ32AG1111",  # read from the government feed
        "22BH1234A",  # BH series
    ],
)
def test_a_real_registration_is_accepted(plate: str) -> None:
    assert normalise_plate(plate).valid is True


@pytest.mark.parametrize(
    "plate",
    [
        "",
        "XX",
        "1234",
        "202RBD",  # read off a camera's on-screen text overlay, not a plate
        "SUVIDHAPARKP3RLVD",  # ditto, in full
        "5000C00",  # an unreadable smear the OCR guessed at
    ],
)
def test_a_malformed_plate_is_refused(plate: str) -> None:
    """The watchlist must reject what the recogniser could never produce.

    A watchlist plate that fails this grammar can never match a detection, because
    detections are normalised through the same function. Accepting one creates an entry
    that looks active on the alert desk and is silently inert -- nobody finds out until
    the vehicle it was watching for passes a camera and nothing happens.
    """
    assert normalise_plate(plate).valid is False


def test_validation_is_the_same_function_the_recogniser_uses() -> None:
    """Guards against a second, drifting copy of the rules appearing later."""
    import inspect

    from services.api.routers import alerts

    source = inspect.getsource(alerts.create_watchlist_entry)
    assert "normalise_plate" in source


# ------------------------------------------------------------ the endpoints


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
    not _reachable(_URL), reason="Postgres not reachable; watchlist endpoint tests need it"
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


def _entry(**over):
    body = {
        "plate_normalised": f"GJ01AB{uuid.uuid4().int % 9000 + 1000}",
        "watchlist_name": "Test list",
        "authority": "SETU test",
        "case_ref": "TEST/2026/1",
        "priority": 10,
        "severity": "low",
        "valid_to": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
    }
    body.update(over)
    return body


@endpoint
def test_a_malformed_plate_is_refused_by_the_api(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.post(
        "/watchlist",
        headers={"Authorization": f"Bearer {token}"},
        json=_entry(plate_normalised="NOTAPLATE!!"),
    )
    assert r.status_code == 422
    # The message must say what a valid plate looks like, not just that this one is not.
    assert "GJ01AB1234" in r.json()["detail"]


@endpoint
def test_an_expired_entry_is_refused_with_plain_wording(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = client.post(
        "/watchlist",
        headers={"Authorization": f"Bearer {token}"},
        json=_entry(valid_to=past),
    )
    assert r.status_code == 422
    assert "future" in r.json()["detail"].lower()


@endpoint
def test_an_entry_can_be_removed_and_the_removal_is_audited(client) -> None:
    """The ledger is the only remaining evidence once the row is gone."""
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/watchlist", headers=headers, json=_entry())
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]
    plate = created.json()["plate_normalised"]

    engine = create_engine(_URL, future=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        before = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'WATCHLIST_ENTRY_REMOVED'")
        ).scalar_one()

    r = client.delete(f"/watchlist/{entry_id}", headers=headers)
    assert r.status_code == 204

    # Gone from the list.
    listed = client.get("/watchlist?include_expired=true", headers=headers).json()
    assert all(e["id"] != entry_id for e in listed)

    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        after = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'WATCHLIST_ENTRY_REMOVED'")
        ).scalar_one()
        latest = (
            conn.execute(
                text(
                    "SELECT detail FROM audit_entry WHERE action = 'WATCHLIST_ENTRY_REMOVED'"
                    " ORDER BY seq DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert after == before + 1
    # The plate must survive in the ledger: it is what the entry was about.
    assert latest is not None and latest["detail"]["plate"] == plate


@endpoint
def test_an_operator_may_not_remove_an_entry(client) -> None:
    admin = _token(client, "admin")
    operator = _token(client, "operator")
    if admin is None or operator is None:
        pytest.skip("credentials not configured")

    created = client.post("/watchlist", headers={"Authorization": f"Bearer {admin}"}, json=_entry())
    assert created.status_code == 201
    entry_id = created.json()["id"]

    r = client.delete(f"/watchlist/{entry_id}", headers={"Authorization": f"Bearer {operator}"})
    assert r.status_code == 403

    # Clean up with the account that is allowed to.
    client.delete(f"/watchlist/{entry_id}", headers={"Authorization": f"Bearer {admin}"})


@endpoint
def test_removing_an_unknown_id_is_a_clean_404(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured")

    r = client.delete(f"/watchlist/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
