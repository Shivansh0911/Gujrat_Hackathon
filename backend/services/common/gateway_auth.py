"""One authenticated HTTP session for everything that talks to the gateway.

The estate moved behind a login: the catalogue and every HLS playlist now answer an
unauthenticated request with a sign-in page rather than an error. That failure is
particularly unhelpful, because a 200 carrying HTML is not something a client naturally
treats as "not signed in" -- the catalogue parser reported it as a missing `cameras`
list, and the HLS resolver reported `not an HLS playlist`. Both read as the feed having
changed format.

So the signal is recognised in one place, the login is performed in one place, and both
callers share a single session. Sharing matters: a session cookie is issued per login,
and letting each caller authenticate separately would mean a login per poll per camera
against infrastructure we are explicitly asked to be gentle with.

Re-authentication is lazy and bounded to one retry. A session expires eventually, and an
expired one looks exactly like never having signed in; retrying once on that signal keeps
a long-running watcher alive across an expiry without risking a login loop.
"""

from __future__ import annotations

import logging
import threading

import requests

from services.common.config import Settings

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SESSION: requests.Session | None = None


def session() -> requests.Session:
    """The shared session. Created once; safe to call from several threads."""
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            s = requests.Session()
            # Some CDNs treat the bare library user-agent as a bot. Naming ourselves is
            # also simply courteous to an operator reading their access log.
            s.headers["User-Agent"] = "SETU/1.0 (Gujarat Police Innovation Challenge)"
            _SESSION = s
        return _SESSION


def looks_like_login(resp: requests.Response) -> bool:
    """True when a response is a sign-in page rather than the thing that was asked for.

    A 401 or 403 is unambiguous. The awkward case is the 200: the gateway serves its
    login form with a success status, so the only tell is that HTML came back where a
    JSON document or a playlist was expected.
    """
    if resp.status_code in (401, 403):
        return True
    if resp.status_code != 200:
        return False
    ctype = (resp.headers.get("Content-Type") or "").lower()
    return "html" in ctype


def login(settings: Settings | None, timeout: float = 20.0) -> bool:
    """Sign in with the configured access code. False when there is nothing to use."""
    if settings is None or not settings.gateway_access_code:
        return False
    url = f"{settings.gateway_scheme}://{settings.gateway_host}{settings.gateway_login_path}"
    log.info("gateway requires authentication; signing in")
    resp = session().post(url, data={"password": settings.gateway_access_code}, timeout=timeout)
    resp.raise_for_status()
    return True


def get(settings: Settings | None, url: str, timeout: float = 20.0) -> requests.Response:
    """GET `url`, signing in once and retrying if the gateway asks us to.

    `settings` may be None, meaning "no credentials available": the request is made
    unauthenticated and whatever comes back is returned. An open estate needs nothing
    more, and a caller with no configuration should not be forced to invent some.
    """
    resp = session().get(url, timeout=timeout)
    if looks_like_login(resp) and login(settings, timeout):
        resp = session().get(url, timeout=timeout)
    return resp
