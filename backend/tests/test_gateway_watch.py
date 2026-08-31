"""The gateway watcher: transitions, and never breaking the page it informs.

Written against a real outage. On 2026-08-31 `live.corp8.cloud` returned a Cloudflare
502 on every endpoint -- the organiser's origin was down, the edge was fine -- and the
console had no way to say so except by someone pressing a button on another page.

What these pin down is the part that is easy to get subtly wrong: `unreachable_since`
must be the moment the outage *started*, not the moment of the most recent failed
poll. Overwriting it every minute would leave the card cheerfully reporting "down for
1 minute" through a four-hour outage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from services.api import gateway_watch


@pytest.fixture(autouse=True)
def fresh_status(monkeypatch: pytest.MonkeyPatch):
    """Each test starts from an unobserved gateway."""
    monkeypatch.setattr(gateway_watch, "_status", gateway_watch.GatewayStatus())


def _probe(reachable: bool, count: int | None = None, error: str | None = None):
    return lambda: (reachable, count, error)


def test_before_the_first_check_the_status_is_unknown_not_down() -> None:
    """`None` is a third state and must not be presented as an outage.

    A cold start, or a free-tier host waking from sleep, has not observed anything
    yet. Reporting that as "unreachable" invents a fact.
    """
    assert gateway_watch.current_status().reachable is None
    assert gateway_watch.current_status().unreachable_since is None


def test_a_reachable_gateway_records_its_catalogue_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(True, 30))
    st = asyncio.run(gateway_watch.check_now())

    assert st.reachable is True
    assert st.cameras_in_catalogue == 30
    assert st.last_success_at is not None
    assert st.unreachable_since is None
    assert st.consecutive_failures == 0


def test_an_outage_records_when_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gateway_watch, "_probe_once", _probe(False, None, "HTTPError: 502 Bad Gateway")
    )
    st = asyncio.run(gateway_watch.check_now())

    assert st.reachable is False
    assert st.unreachable_since is not None
    assert st.last_error is not None and "502" in st.last_error


def test_unreachable_since_is_the_start_of_the_outage_not_the_latest_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the card. Overwriting this each poll would report a
    four-hour outage as "down for 1 minute", every minute, forever."""
    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(False, None, "502"))

    first = asyncio.run(gateway_watch.check_now())
    started = first.unreachable_since
    assert started is not None

    for _ in range(4):
        later = asyncio.run(gateway_watch.check_now())

    assert later.unreachable_since == started
    assert later.consecutive_failures == 5
    assert later.last_checked_at is not None and later.last_checked_at > started


def test_recovery_clears_the_outage_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(False, None, "502"))
    asyncio.run(gateway_watch.check_now())
    assert gateway_watch.current_status().unreachable_since is not None

    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(True, 30))
    st = asyncio.run(gateway_watch.check_now())

    assert st.reachable is True
    assert st.unreachable_since is None
    assert st.consecutive_failures == 0
    assert st.last_error is None


def test_a_second_outage_gets_its_own_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flapping gateway must not report the first outage's start forever."""
    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(False, None, "502"))
    asyncio.run(gateway_watch.check_now())
    first_outage = gateway_watch.current_status().unreachable_since

    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(True, 30))
    asyncio.run(gateway_watch.check_now())

    monkeypatch.setattr(gateway_watch, "_probe_once", _probe(False, None, "502"))
    st = asyncio.run(gateway_watch.check_now())

    assert st.unreachable_since is not None
    assert st.unreachable_since != first_outage


def test_a_probe_that_raises_is_recorded_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watcher that can break the page it informs is worse than no watcher."""

    def boom() -> object:
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(gateway_watch, "_probe_once", boom)

    with pytest.raises(RuntimeError):
        # check_now surfaces it; the *loop* is what must survive, tested below.
        asyncio.run(gateway_watch.check_now())


def test_the_watch_loop_survives_a_failing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bad iteration must not end the watch for the life of the process."""
    calls = {"n": 0}

    async def flaky() -> gateway_watch.GatewayStatus:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return gateway_watch.current_status()

    monkeypatch.setattr(gateway_watch, "check_now", flaky)
    monkeypatch.setattr(gateway_watch, "POLL_INTERVAL_S", 0.01)

    async def run_briefly() -> None:
        task = asyncio.create_task(gateway_watch.watch_forever())
        await asyncio.sleep(0.08)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())
    # It kept going past the exception rather than dying on it.
    assert calls["n"] > 1


def test_an_unconfigured_gateway_is_distinguished_from_a_down_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Nobody told us where the gateway is" and "the gateway is down" are different
    findings, and the operator's next action differs for each."""
    monkeypatch.setattr(
        gateway_watch,
        "_probe_once",
        _probe(False, None, "no gateway configured (SETU_GATEWAY_HOST unset)"),
    )
    st = asyncio.run(gateway_watch.check_now())

    assert st.reachable is False
    assert st.last_error is not None
    assert "SETU_GATEWAY_HOST" in st.last_error


def test_the_serialised_form_is_json_safe() -> None:
    """The endpoint returns this straight to the console."""
    import json

    gateway_watch._status.last_checked_at = datetime.now(timezone.utc)
    payload = gateway_watch.current_status().as_dict()

    json.dumps(payload)  # must not raise
    assert set(payload) >= {
        "reachable",
        "last_checked_at",
        "last_success_at",
        "unreachable_since",
        "poll_interval_s",
    }
