"""An unconfigured camera gateway must not surface as HTTP 500.

`SETU_GATEWAY_HOST` deliberately has no default -- a deployment that forgets it should
not silently fall back to some other team's gateway. But pydantic then raises a
ValidationError the first time a request touches the feed configuration, FastAPI turns
that into a bare 500, and the console shows "Load failed".

That is what happened on the first Render deployment. Two endpoints were affected and
both looked like server faults; the actual cause was one unset environment variable,
and nothing in the response said so. The operator's next move -- read the API logs on a
platform they may not have access to -- is far worse than being told.

These tests pin the distinction the endpoints now make:

    the server broke              -> 500   (still, and correctly, for real faults)
    no gateway is configured      -> a stated, actionable answer
    a gateway is configured but down -> catalogue_reachable = false

The third case was always handled. The middle one was not.
"""

from __future__ import annotations

import pytest

from services.api.routers import cameras


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch):
    """Simulate a deployment with no SETU_GATEWAY_HOST set."""

    def boom() -> object:
        raise ValueError("gateway_host: Field required")

    monkeypatch.setattr(cameras, "get_feed_settings", boom)


def test_missing_configuration_is_reported_not_raised(unconfigured: None) -> None:
    """The helper answers None rather than letting the ValidationError escape."""
    assert cameras._feed_settings_or_none() is None


def test_a_configured_gateway_is_returned_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(cameras, "get_feed_settings", lambda: sentinel)
    assert cameras._feed_settings_or_none() is sentinel


def test_the_helper_swallows_only_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any failure to build the settings means "not configured".

    Deliberately broad: pydantic raises ValidationError, a bad scheme raises ValueError,
    and a missing .env raises something else again. All of them mean the same thing to
    a caller, and none of them is a server fault worth a 500.
    """
    for exc in (ValueError("bad"), RuntimeError("bad"), KeyError("bad")):

        def boom(e: BaseException = exc) -> object:
            raise e

        monkeypatch.setattr(cameras, "get_feed_settings", boom)
        assert cameras._feed_settings_or_none() is None


def test_sync_catalogue_says_what_to_do_about_it(unconfigured: None) -> None:
    """The note must name the variable, not just report failure.

    "Load failed" sent an operator looking for a network problem that did not exist.
    """
    result = cameras.sync_catalogue(session=None, actor=None)  # type: ignore[arg-type]

    assert result.catalogue_reachable is False
    assert result.cameras_in_catalogue == 0
    assert result.note is not None
    assert "SETU_GATEWAY_HOST" in result.note
    # The registry must be described as untouched: a missing config is not authority
    # to conclude every camera has vanished.
    assert "unchanged" in result.note.lower()
