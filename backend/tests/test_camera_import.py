"""Bulk camera onboarding: the row rules, and the endpoint that applies them.

Split in two on purpose, matching how the rest of this suite is organised.

The row rules are pure and are tested directly -- they are the part that decides
whether a camera is placed somewhere real, and they must behave identically for the
seed script and for the API. A rule that disagreed between the two would put two
different registries in front of the same reviewer.

The endpoint tests need a database (RLS, the audit ledger and the CHECK constraint on
`geom` are all enforced in Postgres, not in Python) and skip without one, exactly as
`test_row_level_security.py` does. Running them against SQLite would prove the
handler's control flow while silently dropping every guarantee that matters.
"""

from __future__ import annotations

import csv
import io
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from services.registry.camera_import import validate_row, validate_rows

# --------------------------------------------------------------------- row rules


def _row(**overrides: str) -> dict[str, str]:
    """A valid row, so each test can name only the field it is about."""
    base = {
        "camera_ref": "TEST-1",
        "location_text": "Bhavani Char Rasta",
        "lat": "23.0225",
        "lon": "72.5714",
        "geom_source": "approximate",
        "confidence_radius_m": "5000",
        "resolved_by": "nominatim:Ahmedabad",
        "resolved_at": "2026-08-26T00:56:21.543081+00:00",
    }
    base.update(overrides)
    return base


def test_a_well_formed_row_has_no_problems() -> None:
    assert validate_row(_row(), 2) == []


def test_unset_without_coordinates_is_valid() -> None:
    """The honest representation of "we do not know where this camera is"."""
    row = _row(lat="", lon="", geom_source="unset", confidence_radius_m="")
    assert validate_row(row, 2) == []


def test_empty_camera_ref_is_rejected() -> None:
    problems = validate_row(_row(camera_ref=""), 7)
    assert any("empty camera_ref" in p for p in problems)
    assert all(p.startswith("line 7") for p in problems)


def test_a_shifted_column_is_named_as_the_likely_cause() -> None:
    """The real failure mode: an unquoted comma moves every later field along one.

    A latitude then lands in `geom_source`, and without this check the camera would be
    placed at a coordinate belonging to nothing -- an authoritative-looking position
    that is simply wrong, which is worse than no position at all.
    """
    problems = validate_row(_row(geom_source="23.0225"), 4)
    assert len(problems) == 1
    assert "is not one of" in problems[0]
    assert "shifted column" in problems[0]


def test_unset_with_coordinates_is_contradictory() -> None:
    problems = validate_row(_row(geom_source="unset"), 2)
    assert any("geom_source=unset but coordinates present" in p for p in problems)


def test_a_source_that_promises_coordinates_must_supply_them() -> None:
    problems = validate_row(_row(lat="", lon=""), 2)
    assert any("but no coordinates" in p for p in problems)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("lat", "not-a-number", "is not a number"),
        ("lon", "not-a-number", "is not a number"),
        ("lat", "91.0", "out of range"),
        ("lat", "-91.0", "out of range"),
        ("lon", "181.0", "out of range"),
        ("lon", "-181.0", "out of range"),
    ],
)
def test_coordinates_are_range_checked(field: str, value: str, expected: str) -> None:
    problems = validate_row(_row(**{field: value}), 2)
    assert any(expected in p and field in p for p in problems)


def test_a_non_positive_radius_is_rejected() -> None:
    """A radius of zero claims certainty the geocoder did not provide."""
    assert any("must be > 0" in p for p in validate_row(_row(confidence_radius_m="0"), 2))
    assert any("invalid" in p for p in validate_row(_row(confidence_radius_m="wide"), 2))


def test_problems_carry_the_line_number_for_every_row() -> None:
    rows = [_row(camera_ref=""), _row(), _row(lat="999")]
    problems = validate_rows(rows)
    # Rows are numbered from 2 because the header occupies line 1.
    assert any(p.startswith("line 2") for p in problems)
    assert any(p.startswith("line 4") for p in problems)
    assert not any(p.startswith("line 3") for p in problems)


def test_the_committed_seed_file_still_passes_its_own_rules() -> None:
    """The shipped seed data must satisfy the rules the API enforces on uploads."""
    from services.common.paths import SEED_DIR

    seed = SEED_DIR / "camera_geo.csv"
    if not seed.exists():
        pytest.skip("camera_geo.csv not present")
    rows = list(csv.DictReader(seed.open(encoding="utf-8")))
    assert validate_rows(rows) == []


# ---------------------------------------------------------------- the endpoint


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
    not _reachable(_URL),
    reason="Postgres not reachable; bulk-import endpoint tests need the compose database",
)


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(_row().keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from services.api.main import app

    return TestClient(app)


def _token(client, role: str) -> str | None:
    """Log in with whichever credentials the running stack was configured with."""
    from services.api.config import get_api_settings

    settings = get_api_settings()
    password = settings.admin_password if role == "admin" else settings.operator_password
    if not password:
        return None
    r = client.post("/auth/login", data={"username": role, "password": password})
    if r.status_code != 200:
        return None
    return str(r.json()["access_token"])


@endpoint
def test_a_valid_csv_is_accepted_and_creates_cameras(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured for this stack")

    ref = f"IMPORT-{uuid.uuid4().hex[:8]}"
    body = _csv_bytes([_row(camera_ref=ref)])

    r = client.post(
        "/cameras/bulk-import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("cameras.csv", body, "text/csv")},
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["rows_read"] == 1
    assert result["accepted"] == 1
    assert result["rejected"] == 0
    assert result["created"] == 1
    assert result["rejections"] == []


@endpoint
def test_a_malformed_row_is_rejected_with_its_reason(client) -> None:
    """The good row still lands. Partial success is the point of the endpoint."""
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured for this stack")

    good = f"IMPORT-{uuid.uuid4().hex[:8]}"
    body = _csv_bytes([_row(camera_ref=good), _row(camera_ref="BAD-1", lat="999")])

    r = client.post(
        "/cameras/bulk-import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("cameras.csv", body, "text/csv")},
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["accepted"] == 1
    assert result["rejected"] == 1

    (rejection,) = result["rejections"]
    assert rejection["line"] == 3  # header is line 1, the good row is line 2
    assert rejection["camera_ref"] == "BAD-1"
    assert any("out of range" in reason for reason in rejection["reasons"])


@endpoint
def test_an_operator_may_not_import(client) -> None:
    """Onboarding asserts where surveillance exists; that is an admin act."""
    token = _token(client, "operator")
    if token is None:
        pytest.skip("no operator credentials configured for this stack")

    r = client.post(
        "/cameras/bulk-import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("cameras.csv", _csv_bytes([_row()]), "text/csv")},
    )
    assert r.status_code == 403


@endpoint
def test_an_anonymous_caller_may_not_import(client) -> None:
    r = client.post(
        "/cameras/bulk-import",
        files={"file": ("cameras.csv", _csv_bytes([_row()]), "text/csv")},
    )
    assert r.status_code in (401, 403)


@endpoint
def test_a_csv_without_the_expected_header_is_refused(client) -> None:
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured for this stack")

    r = client.post(
        "/cameras/bulk-import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("notes.csv", b"colour,make\nred,tata\n", "text/csv")},
    )
    assert r.status_code == 422
    assert "camera_ref" in r.json()["detail"]


@endpoint
def test_the_import_is_written_to_the_audit_ledger(client) -> None:
    """A mutating endpoint that is not audited is a gap in the evidence chain."""
    token = _token(client, "admin")
    if token is None:
        pytest.skip("no admin credentials configured for this stack")

    engine = create_engine(_URL, future=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        before = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'CAMERA_BULK_IMPORT'")
        ).scalar_one()

    ref = f"IMPORT-{uuid.uuid4().hex[:8]}"
    r = client.post(
        "/cameras/bulk-import",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("cameras.csv", _csv_bytes([_row(camera_ref=ref)]), "text/csv")},
    )
    assert r.status_code == 200, r.text

    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
        after = conn.execute(
            text("SELECT count(*) FROM audit_entry WHERE action = 'CAMERA_BULK_IMPORT'")
        ).scalar_one()
        latest = (
            conn.execute(
                text(
                    "SELECT actor_role, detail FROM audit_entry"
                    " WHERE action = 'CAMERA_BULK_IMPORT' ORDER BY seq DESC LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
    engine.dispose()

    assert after == before + 1
    assert latest is not None
    assert latest["actor_role"] == "admin"
    assert latest["detail"]["accepted"] == 1


def test_a_department_column_is_read_rather_than_ignored():
    """The import calls itself a departmental spreadsheet and used to file every row
    under the default, discarding the one column that says whose camera it is."""
    import inspect

    from services.api.routers import cameras as mod

    src = inspect.getsource(mod.bulk_import_cameras)
    assert 'row.get("department_code")' in src, "the column is not read"
    assert "unknown_departments" in src, "an unrecognised code is not reported back"


def test_an_omitted_department_does_not_move_existing_cameras():
    """Silence is not an instruction. An import without the column must leave every
    existing camera where it is, rather than sweeping the estate into the default."""
    import inspect

    from services.api.routers import cameras as mod

    src = inspect.getsource(mod.bulk_import_cameras)
    body = src[src.index("            updated += 1") :]
    guarded = "if department is not None:" in body[: body.index("location =")]
    assert guarded, "department is reassigned on update without checking it was given"
