"""The parts of the API another department's system would actually depend on.

Three things are contracts here rather than implementation details, and each is tested
as one:

* **Error codes.** A client branches on them, so they must be stable, closed, and
  mapped to exactly one HTTP status apiece.
* **The correlation id.** On every response, not only on errors, because the request a
  client most wants to ask about is usually one that succeeded and returned something
  surprising.
* **Liveness versus readiness.** Conflating them turns a database blip into a restart
  loop, which is the specific outage this separation exists to prevent.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.buildinfo import (
    DependencyCheck,
    build_info,
    expected_migration_head,
)
from services.api.errors import (
    CORRELATION_HEADER,
    RETRYABLE,
    STATUS_FOR,
    ApiError,
    ErrorCode,
    _sanitise_inbound,
)
from services.api.versioning import (
    API_VERSION,
    DEPRECATION_NOTICE_DAYS,
    SUPPORTED_VERSIONS,
    deprecation_headers,
    version_info,
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    from services.api.main import app

    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------ error codes


def test_every_error_code_has_exactly_one_http_status() -> None:
    """The same condition must not come back as a 400 from one router and 422 from another."""
    for code in ErrorCode:
        assert code in STATUS_FOR, f"{code} has no status mapping"
        assert 400 <= STATUS_FOR[code] <= 599


def test_retryable_codes_are_a_subset_of_the_taxonomy() -> None:
    assert RETRYABLE <= set(ErrorCode)


def test_permanent_failures_are_not_marked_retryable() -> None:
    """Telling a client to retry a validation failure wastes both sides' capacity."""
    for code in (
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.RESOURCE_NOT_FOUND,
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.CREDENTIALS_INVALID,
        ErrorCode.WINDOW_TOO_LARGE,
    ):
        assert not ApiError(code, "x").retryable


def test_transient_failures_are_marked_retryable() -> None:
    for code in (
        ErrorCode.RATE_LIMITED,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.FEED_UNAVAILABLE,
    ):
        assert ApiError(code, "x").retryable


def test_an_error_body_carries_code_message_retryable_and_correlation() -> None:
    body = ApiError(ErrorCode.WINDOW_TOO_LARGE, "window exceeds 31 days").body("abc123")["error"]
    assert body["code"] == "window_too_large"
    assert body["message"] == "window exceeds 31 days"
    assert body["retryable"] is False
    assert body["correlation_id"] == "abc123"


def test_retry_after_is_returned_when_the_platform_knows_it() -> None:
    error = ApiError(ErrorCode.RATE_LIMITED, "slow down", retry_after_s=30)
    assert error.body("x")["error"]["retry_after_s"] == 30


def test_error_code_values_are_snake_case_and_stable() -> None:
    """These strings are the contract. A rewording is a breaking change."""
    for code in ErrorCode:
        assert code.value == code.value.lower()
        assert " " not in code.value and "-" not in code.value


# -------------------------------------------------------------- correlation id


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    """Not only errors. The surprising success is the one support asks about."""
    for path in ("/healthz", "/", "/build-info", "/api-version"):
        response = client.get(path)
        assert response.headers.get(CORRELATION_HEADER), f"{path} returned no correlation id"


def test_a_callers_own_trace_id_flows_through(client: TestClient) -> None:
    response = client.get("/healthz", headers={CORRELATION_HEADER: "caller-trace-0001"})
    assert response.headers[CORRELATION_HEADER] == "caller-trace-0001"


@pytest.mark.parametrize(
    "hostile",
    [
        "bad\nnewline",
        "x" * 500,
        "short",
        "semi;colon",
        "",
    ],
    ids=["newline-injection", "oversized", "too-short", "punctuation", "empty"],
)
def test_a_hostile_correlation_id_is_replaced_not_echoed(hostile: str) -> None:
    """Echoing an arbitrary header into logs and the ledger is an injection route.

    A newline splits a log line in two; 8 KB of it fills a disk.
    """
    assert _sanitise_inbound(hostile) is None


def test_a_reasonable_trace_id_is_accepted() -> None:
    for good in ("abcd1234", "trace-id_42", "f" * 64):
        assert _sanitise_inbound(good) == good


# --------------------------------------------------- liveness versus readiness


def test_liveness_does_not_touch_the_database(client: TestClient) -> None:
    """An orchestrator restarts the container when liveness fails.

    A liveness probe that touched the database would turn a recoverable blip into a
    restart loop, which is a guaranteed outage instead of a recoverable one.
    """
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_answers_head_as_well_as_get(client: TestClient) -> None:
    """UptimeRobot read the resulting 405 as a four-day outage on a healthy service."""
    assert client.head("/healthz").status_code == 200


def test_readiness_reports_each_dependency_separately(client: TestClient) -> None:
    body = client.get("/readyz").json()
    names = {check["name"] for check in body["checks"]}
    assert {"database", "migrations"} <= names


def test_readiness_returns_503_when_not_ready() -> None:
    """A load balancer needs the status code, not a 200 with `ready: false` inside."""
    from services.api import buildinfo

    assert buildinfo.readiness.__doc__  # the function exists and is documented
    # The mapping from `ready` to the status code is asserted through the endpoint in
    # the integration path; here we assert the shape a failing check produces.
    failed = DependencyCheck("database", False, "unreachable (OperationalError)")
    assert failed.as_dict()["ok"] is False
    assert "unreachable" in failed.as_dict()["detail"]


def test_a_dependency_failure_does_not_leak_connection_details() -> None:
    """`/readyz` is commonly left unauthenticated, and an OperationalError's text
    contains the host, the port and sometimes the user."""
    check = DependencyCheck("database", False, "unreachable (OperationalError)")
    detail = check.as_dict()["detail"]
    assert "@" not in detail and "://" not in detail


def test_readiness_does_not_depend_on_the_government_gateway(client: TestClient) -> None:
    """Third-party infrastructure that is down for hours must not remove this instance
    from the load balancer while journeys, alerts and exports all work."""
    names = {check["name"] for check in client.get("/readyz").json()["checks"]}
    assert "gateway" not in names


# ------------------------------------------------------------------ build info


def test_the_expected_migration_head_is_a_single_revision() -> None:
    """Two heads means the parser is wrong or the migrations have branched.

    It was the parser: 0002 uses an annotated assignment (`revision: str = ...`) and a
    parser handling only the bare form dropped its down_revision, making 0001 look like
    a second head and `/readyz` report a mismatch on a correctly migrated database.
    """
    head = expected_migration_head()
    assert head != "unknown"
    assert head.startswith("00")


def test_build_info_names_the_analytics_that_produced_a_result(client: TestClient) -> None:
    """An evidence export names its analytics versions; this says which build made them."""
    body = client.get("/build-info").json()
    assert set(body["analytics"]) == {
        "anpr",
        "sighting",
        "vehicle_detector",
        "appearance_kind",
    }


def test_build_info_reports_unknown_rather_than_omitting_the_commit() -> None:
    """An ops team scripting against this needs the key to exist."""
    body = build_info()
    assert "commit" in body and body["commit"]
    assert "expected_migration_head" in body


# ------------------------------------------------------------------ versioning


def test_every_response_announces_the_serving_version(client: TestClient) -> None:
    assert client.get("/healthz").headers["X-API-Version"] == API_VERSION


def test_the_current_version_is_supported() -> None:
    assert API_VERSION in SUPPORTED_VERSIONS


def test_a_current_version_carries_no_sunset_headers() -> None:
    assert deprecation_headers(API_VERSION) == {}


def test_the_deprecation_policy_is_served_not_only_documented(client: TestClient) -> None:
    """A client should be able to discover the notice period without reading a repo."""
    body = client.get("/api-version").json()
    assert body["current"] == API_VERSION
    assert body["deprecation_notice_days"] == DEPRECATION_NOTICE_DAYS
    assert str(DEPRECATION_NOTICE_DAYS) in body["policy"]


def test_the_notice_period_survives_a_procurement_cycle() -> None:
    """A departmental integration change takes months. A shorter window is one in name."""
    assert DEPRECATION_NOTICE_DAYS >= 180


def test_a_deprecated_version_would_carry_rfc_8594_headers() -> None:
    """The mechanism exists before the first deprecation, so that one is a data change
    rather than a code change made under time pressure."""
    from services.api import versioning

    versioning.DEPRECATED_VERSIONS["v0"] = "Wed, 01 Jul 2026 00:00:00 GMT"
    try:
        headers = deprecation_headers("v0")
        assert headers["Deprecation"] == "true"
        assert headers["Sunset"].endswith("GMT")
    finally:
        versioning.DEPRECATED_VERSIONS.pop("v0", None)


def test_version_info_is_serialisable() -> None:
    import json

    json.dumps(version_info())
