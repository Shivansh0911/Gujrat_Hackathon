"""Rate limiting on the one unauthenticated endpoint that does expensive work.

`POST /auth/login` verifies with bcrypt, which is deliberately slow. Without a limit
an unauthenticated caller can make the server burn CPU on demand: cheap to send,
expensive to answer.
"""

from __future__ import annotations

import pytest

from services.api.rate_limit import SlidingWindowLimiter


def test_attempts_within_the_limit_are_allowed() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_s=60)
    for _ in range(3):
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed is True


def test_the_attempt_past_the_limit_is_refused() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_s=60)
    for _ in range(3):
        limiter.check("1.2.3.4")
    allowed, retry_after = limiter.check("1.2.3.4")
    assert allowed is False
    assert 0 < retry_after <= 60


def test_callers_are_counted_separately() -> None:
    """One noisy source must not lock everyone else out."""
    limiter = SlidingWindowLimiter(limit=2, window_s=60)
    for _ in range(2):
        limiter.check("1.2.3.4")
    assert limiter.check("1.2.3.4")[0] is False
    assert limiter.check("5.6.7.8")[0] is True


def test_the_window_slides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts that have aged out must stop counting."""
    now = [1000.0]
    monkeypatch.setattr("services.api.rate_limit.time.monotonic", lambda: now[0])

    limiter = SlidingWindowLimiter(limit=2, window_s=60)
    assert limiter.check("1.2.3.4")[0] is True
    assert limiter.check("1.2.3.4")[0] is True
    assert limiter.check("1.2.3.4")[0] is False

    now[0] += 61
    assert limiter.check("1.2.3.4")[0] is True


def test_hammering_does_not_extend_the_lockout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused attempt is not recorded.

    Counting refusals would let a caller who keeps retrying push their own window
    forward indefinitely -- turning the limiter into a denial of service against the
    person it is meant to slow down, including a legitimate user behind a shared IP.
    """
    now = [1000.0]
    monkeypatch.setattr("services.api.rate_limit.time.monotonic", lambda: now[0])

    limiter = SlidingWindowLimiter(limit=1, window_s=10)
    assert limiter.check("1.2.3.4")[0] is True

    # Hammer while locked out.
    for _ in range(20):
        now[0] += 0.1
        assert limiter.check("1.2.3.4")[0] is False

    # The original attempt ages out on schedule regardless of the hammering.
    now[0] = 1011.0
    assert limiter.check("1.2.3.4")[0] is True


def test_a_zero_limit_is_rejected() -> None:
    """A limiter that allows nothing is a configuration mistake, not a policy."""
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=0, window_s=60)
