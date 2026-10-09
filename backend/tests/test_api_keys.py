"""Service accounts: what a key can do, what it cannot, and what it never reveals.

The sharpest test in this file is `test_an_estate_wide_key_cannot_reach_admin_endpoints`,
because that property is the entire justification for a key never carrying the admin
role. Its sibling -- that an estate-wide key *can* read across departments -- is the
property that was quietly broken when visibility was derived from the role: such a key
bound NO_DEPARTMENT and every query returned zero rows, which reads as "there is no
data" rather than "you cannot see it". Both directions are pinned.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.api.api_keys import (
    ALL_SCOPES,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    KEY_PREFIX,
    ApiKeyError,
    KeyPrincipal,
    Scope,
    authenticate,
    generate_token,
    issue,
    list_keys,
    record_use,
    revoke,
)
from services.api.security import Actor


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
needs_db = pytest.mark.skipif(not _reachable(_URL), reason="Postgres not reachable")


@pytest.fixture
def session() -> Session:
    engine = create_engine(_URL or "", future=True)
    s: Session = sessionmaker(bind=engine, future=True)()
    s.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    yield s
    s.rollback()
    s.close()
    engine.dispose()


@pytest.fixture
def department(session: Session) -> uuid.UUID:
    dept = uuid.uuid4()
    session.execute(
        text("INSERT INTO department (id, code, name) VALUES (:d, :c, 'Key Dept')"),
        {"d": dept, "c": f"KEY_{uuid.uuid4().hex[:8]}"},
    )
    session.flush()
    return dept


# ------------------------------------------------------------- token properties


def test_a_token_carries_a_recognisable_prefix() -> None:
    """Secret scanners key off exactly this kind of marker, and a human pasting one
    into a ticket can tell what they have pasted."""
    token, _, _ = generate_token()
    assert token.startswith(KEY_PREFIX)


def test_tokens_are_high_entropy_and_unique() -> None:
    tokens = {generate_token()[0] for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) > 40 for t in tokens)


def test_the_stored_hash_is_not_the_token() -> None:
    token, token_hash, visible = generate_token()
    assert token_hash != token
    assert len(token_hash) == 64
    assert token_hash not in token
    # The clear-text portion identifies but cannot authenticate.
    assert visible in token and len(visible) == 8


# ------------------------------------------------------------------- issuance


@needs_db
def test_a_key_round_trips_and_authenticates(session: Session, department: uuid.UUID) -> None:
    issued = issue(
        session,
        name="Traffic dashboard",
        scopes=[Scope.READ_DETECTIONS.value],
        department_id=department,
        created_by="admin",
    )
    principal = authenticate(session, issued.token)
    assert principal.name == "Traffic dashboard"
    assert principal.department_id == department
    assert principal.has(Scope.READ_DETECTIONS)
    assert not principal.has(Scope.WRITE_IMPORT)


@needs_db
def test_an_unknown_scope_is_refused_rather_than_dropped(
    session: Session, department: uuid.UUID
) -> None:
    """A typo'd scope silently ignored produces a key that appears to work until it
    reaches the endpoint it was meant for."""
    with pytest.raises(ApiKeyError, match="unknown scopes"):
        issue(
            session,
            name="typo",
            scopes=["read:detection"],  # singular; not a scope
            department_id=department,
            created_by="admin",
        )


@needs_db
def test_a_key_with_no_scopes_is_refused(session: Session, department: uuid.UUID) -> None:
    with pytest.raises(ApiKeyError, match="at least one"):
        issue(session, name="useless", scopes=[], department_id=department, created_by="admin")


@needs_db
def test_a_key_needs_a_name_a_human_will_recognise(session: Session, department: uuid.UUID) -> None:
    with pytest.raises(ApiKeyError, match="needs a name"):
        issue(
            session,
            name="   ",
            scopes=[Scope.READ_ALERTS.value],
            department_id=department,
            created_by="admin",
        )


# -------------------------------------------------------------------- refusal


@needs_db
def test_a_revoked_key_stops_authenticating(session: Session, department: uuid.UUID) -> None:
    issued = issue(
        session,
        name="to be revoked",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    assert revoke(session, issued.id, revoked_by="admin") is True
    with pytest.raises(ApiKeyError):
        authenticate(session, issued.token)


@needs_db
def test_revocation_is_not_a_delete(session: Session, department: uuid.UUID) -> None:
    """A revoked key's audit history must keep resolving to something, and "when was
    this turned off, by whom" is a question an incident review asks."""
    issued = issue(
        session,
        name="historical",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    revoke(session, issued.id, revoked_by="auditor")
    row = next(k for k in list_keys(session) if k["key_prefix"] == issued.key_prefix)
    assert row["revoked_at"] is not None
    assert row["revoked_by"] == "auditor"
    assert row["active"] is False


@needs_db
def test_revoking_twice_reports_nothing_to_do(session: Session, department: uuid.UUID) -> None:
    issued = issue(
        session,
        name="twice",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    assert revoke(session, issued.id, revoked_by="a") is True
    assert revoke(session, issued.id, revoked_by="b") is False


@needs_db
def test_an_expired_key_stops_authenticating(session: Session, department: uuid.UUID) -> None:
    issued = issue(
        session,
        name="expiring",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
        expires_in_days=1,
    )
    session.execute(
        text("UPDATE api_key SET expires_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": issued.id},
    )
    with pytest.raises(ApiKeyError):
        authenticate(session, issued.token)


@needs_db
def test_every_refusal_looks_the_same_from_outside(session: Session, department: uuid.UUID) -> None:
    """Distinguishing "no such key" from "revoked" would let the holder of a dead token
    learn whether it ever existed. The legitimate holder already knows -- they were told."""
    issued = issue(
        session,
        name="revoked soon",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    revoke(session, issued.id, revoked_by="admin")

    messages = set()
    for token in (issued.token, f"{KEY_PREFIX}completely-made-up-value"):
        with pytest.raises(ApiKeyError) as exc:
            authenticate(session, token)
        messages.add(str(exc.value))
    assert len(messages) == 1, f"refusals differ and leak existence: {messages}"


def test_a_token_without_the_prefix_is_not_even_looked_up() -> None:
    """A JWT arriving here would otherwise cause a pointless hash and table read."""
    with pytest.raises(ApiKeyError, match="not a SETU API key"):
        authenticate(None, "eyJhbGciOiJIUzI1NiJ9.abc.def")  # type: ignore[arg-type]


# ------------------------------------------------------------------- no leaks


@needs_db
def test_listing_keys_never_returns_a_token_or_a_hash(
    session: Session, department: uuid.UUID
) -> None:
    issued = issue(
        session,
        name="listed",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    for row in list_keys(session):
        assert "token" not in row
        assert "token_hash" not in row
        assert issued.token not in str(row)


def test_the_issued_key_repr_does_not_print_the_token() -> None:
    """Any traceback, any debugger, any structlog field rendering the dataclass."""
    from services.api.api_keys import IssuedKey

    key = IssuedKey(
        id=uuid.uuid4(),
        token="setu_sk_very-secret-value",
        key_prefix="very-sec",
        name="x",
        scopes=("read:alerts",),
        department_id=None,
        expires_at=None,
    )
    assert "very-secret-value" not in repr(key)
    assert "shown once" in repr(key)


# ------------------------------------------------------------------ usage


@needs_db
def test_use_is_recorded_so_a_dormant_key_can_be_retired(
    session: Session, department: uuid.UUID
) -> None:
    """`last_used_at` is the column that makes retirement possible: a key nothing has
    presented in six months is one somebody can safely turn off."""
    issued = issue(
        session,
        name="used",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    principal = authenticate(session, issued.token)
    record_use(session, principal)
    record_use(session, principal)
    session.flush()

    row = next(k for k in list_keys(session) if k["key_prefix"] == issued.key_prefix)
    assert row["use_count"] >= 2
    assert row["last_used_at"] is not None


@needs_db
def test_a_key_without_its_own_ceiling_gets_the_platform_default(
    session: Session, department: uuid.UUID
) -> None:
    issued = issue(
        session,
        name="default rate",
        scopes=[Scope.READ_ALERTS.value],
        department_id=department,
        created_by="admin",
    )
    assert authenticate(session, issued.token).rate_limit_per_minute == (
        DEFAULT_RATE_LIMIT_PER_MINUTE
    )


# --------------------------------------------- visibility versus authorisation


def _key_actor(department_id: uuid.UUID | None) -> Actor:
    return Actor(
        subject="apikey:abcd1234",
        role="operator",
        department_id=department_id,
        api_key_id=uuid.uuid4(),
        scopes=frozenset({Scope.READ_ALERTS.value}),
    )


def test_an_estate_wide_key_sees_every_department() -> None:
    """The property that was silently broken when visibility came from the role.

    Such a key bound NO_DEPARTMENT and every query returned zero rows -- which reads as
    "there is no data" rather than "you cannot see it", and is the worse of the two.
    """
    assert _key_actor(None).sees_all_departments is True


def test_a_departmental_key_sees_only_its_own() -> None:
    assert _key_actor(uuid.uuid4()).sees_all_departments is False


def test_an_estate_wide_key_is_still_not_an_admin() -> None:
    """Visibility and authorisation are different questions.

    Granting the admin *role* to express estate-wide reach would additionally hand the
    key every mutating admin endpoint, which is not what read-across-departments means.
    """
    actor = _key_actor(None)
    assert actor.sees_all_departments is True
    assert actor.is_admin is False


def test_a_human_operator_without_a_department_still_fails_closed() -> None:
    """The fix must not widen anything except the case it was written for."""
    human = Actor(subject="operator", role="operator", department_id=None)
    assert human.sees_all_departments is False
    assert human.is_service_account is False


def test_a_human_admin_keeps_full_visibility() -> None:
    assert Actor(subject="admin", role="admin", department_id=None).sees_all_departments is True


def test_a_human_session_is_not_scope_limited() -> None:
    """Scopes narrow a machine integration. Retrofitting them onto human roles would be
    a second authorisation system saying different things about the same person."""
    assert Actor(subject="operator", role="operator", department_id=None).scopes is None


# ----------------------------------------------------------------- the scope set


def test_the_scope_set_is_small_enough_to_configure_correctly() -> None:
    """A permission system with forty scopes is one nobody configures correctly, and its
    failure mode is an integration granted everything because narrowing it was fiddly."""
    assert len(ALL_SCOPES) <= 8


def test_every_scope_is_namespaced_by_verb() -> None:
    for scope in ALL_SCOPES:
        verb, _, noun = scope.partition(":")
        assert verb in {"read", "write", "manage"}, scope
        assert noun, scope


def test_a_principal_reports_estate_wide_reach() -> None:
    principal = KeyPrincipal(
        id=uuid.uuid4(),
        name="x",
        key_prefix="abcd1234",
        department_id=None,
        scopes=frozenset(ALL_SCOPES),
        rate_limit_per_minute=10,
    )
    assert principal.is_estate_wide is True
    assert principal.audit_subject == "apikey:abcd1234"
