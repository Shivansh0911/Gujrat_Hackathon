"""A small sliding-window rate limiter for unauthenticated endpoints.

Written for one endpoint in particular. `POST /auth/login` verifies with bcrypt,
which is deliberately slow -- that is the whole point of it as a password hash. It
also means an unauthenticated caller can make the server burn CPU on demand, and a
handful of concurrent attackers can saturate it without ever guessing a password.
The cost asymmetry is the vulnerability: cheap to send, expensive to answer.

Rate limiting also blunts credential stuffing, but that is the lesser reason here.
The passwords this deployment issues are 18+ characters of `token_urlsafe`, so
guessing is not the realistic threat; exhausting the CPU is.

**What this is not.** State lives in this process. The container runs uvicorn with
`SETU_WORKERS` workers, defaulting to 2, and each holds its own window -- so the
effective ceiling is the configured limit times the worker count, and which worker
answers a given request is up to the kernel. Measured on the running stack: 35 rapid
attempts got through before a 429, against a nominal limit of 30.

That is an acceptable trade at this size, because the property that matters is the
ceiling existing at all rather than its exact value: unbounded bcrypt is the
vulnerability, 60/minute is not. It is not a distributed limiter, and it should be
replaced by one backed by shared storage before this runs across instances. Recorded
here rather than discovered later.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Allow `limit` events per `window_s`, keyed by caller."""

    def __init__(self, limit: int, window_s: float) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, float]:
        """Record an attempt. Returns `(allowed, retry_after_seconds)`."""
        now = time.monotonic()
        hits = self._hits[key]

        # Drop everything that has fallen out of the window before deciding, so a
        # caller is never held back by attempts that have already aged out.
        cutoff = now - self._window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self._limit:
            # Refusals are not recorded. Counting them would let a caller who keeps
            # hammering extend their own lockout indefinitely, which turns a limiter
            # into a self-inflicted denial of service.
            return False, max(0.0, hits[0] + self._window_s - now)

        hits.append(now)
        return True, 0.0

    def reset(self, key: str | None = None) -> None:
        """Forget one caller, or all of them. Used by tests."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


#: Login attempts per IP per minute.
#:
#: Set well above any legitimate pattern -- a person mistyping a password a few times,
#: or a verification script logging in as both accounts -- because the goal is to stop
#: an automated flood, not to punish someone who fumbles their password. It still caps
#: bcrypt work per source at a level a single core absorbs comfortably.
LOGIN_LIMIT = 30
LOGIN_WINDOW_S = 60.0

login_limiter = SlidingWindowLimiter(LOGIN_LIMIT, LOGIN_WINDOW_S)

__all__ = ["SlidingWindowLimiter", "login_limiter", "LOGIN_LIMIT", "LOGIN_WINDOW_S"]
