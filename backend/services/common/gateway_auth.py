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

#: How many times to sign in and try again before giving the caller the sign-in page.
#: One was not enough. A worker serving its first gateway request has no cookie, and
#: uvicorn handles requests concurrently, so two threads can both see the sign-in page,
#: both sign in, and one still be holding the older response when it retries. Measured
#: on the deployed instance: sporadic 502s right after a redeploy, then 10 of 10
#: playlists once warm. Two attempts covers the cold start; more would be a login loop
#: against infrastructure we do not own.
_LOGIN_ATTEMPTS = 2

#: Why the last sign-in was refused, when the estate gave a reason worth repeating.
#: The refusal arrives on the login itself while the original request comes back as an
#: ordinary sign-in page, so without carrying it the caller only ever sees "not signed
#: in" and reports an outage.
_last_refusal: str | None = None

_LOCK = threading.Lock()
_SESSION: requests.Session | None = None


def session() -> requests.Session:
    """The shared session. Created once; safe to call from several threads."""
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            s = requests.Session()
            # The estate refuses its media plane to anything that does not look like a
            # browser: `GET /cam01/index.m3u8` with a bare library user-agent answers
            # `403 browser required`, and because 403 also means "sign in", the client
            # signed in again and was refused again. Measured directly -- our previous
            # `SETU/1.0 (...)` string gets 403, a browser string gets the playlist.
            #
            # Our identity is kept on the end rather than dropped. That was tested too,
            # not assumed: the combined string returns a real playlist, so an operator
            # reading their access log can still tell who we are.
            s.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 SETU/1.0"
            )
            _SESSION = s
        return _SESSION


#: The estate meters viewing per account and refuses everything, catalogue included,
#: once the budget is spent. It says so in a plain-text body behind a 403.
_QUOTA_MARKERS = ("watch time limit", "cooldown")


def looks_exhausted(resp: requests.Response) -> str | None:
    """The estate's watch-time quota message, or None.

    Worth separating from a sign-in failure because the two call for opposite
    responses. Signing in again is the right move when a session has lapsed and the
    exactly wrong one here: the quota is per account, so every retry spends more of a
    budget that is already empty, and the caller is told "unreachable" about an estate
    that is working and simply metering us.
    """
    if resp.status_code != 403:
        return None
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/plain" not in ctype:
        return None
    body = (resp.text or "")[:200]
    low = body.lower()
    return body.strip() if any(m in low for m in _QUOTA_MARKERS) else None


def looks_like_login(resp: requests.Response) -> bool:
    """True when a response is a sign-in page rather than the thing that was asked for.

    A 401 or 403 usually is, with one exception that costs real money: a 403 carrying
    the estate's watch-time message is a quota refusal, not a lapsed session, and
    treating it as one sends us back to the login form to spend more quota.
    """
    if resp.status_code in (401, 403):
        return looks_exhausted(resp) is None
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
    # The sign-in grew an email field on 2026-09-03. It is sent only when configured,
    # because an estate whose form has one field rejects a POST carrying two.
    form: dict[str, str] = {"password": settings.gateway_access_code}
    if settings.gateway_email:
        form["email"] = settings.gateway_email
    resp = session().post(url, data=form, timeout=timeout)
    # A sign-in refused for quota is not a sign-in failure to raise about: the estate
    # meters viewing per account and refuses the login itself once the budget is spent.
    # Raising here buried the message under a bare 403 and sent the caller looking for
    # a fault that does not exist.
    global _last_refusal
    spent = looks_exhausted(resp)
    if spent:
        log.warning("gateway refused the sign-in: %s", spent)
        _last_refusal = spent
        return False
    resp.raise_for_status()
    _last_refusal = None
    return True


def last_refusal() -> str | None:
    """The estate's reason for the most recent refused sign-in, if it gave one."""
    return _last_refusal


def get(settings: Settings | None, url: str, timeout: float = 20.0) -> requests.Response:
    """GET `url`, signing in once and retrying if the gateway asks us to.

    `settings` may be None, meaning "no credentials available": the request is made
    unauthenticated and whatever comes back is returned. An open estate needs nothing
    more, and a caller with no configuration should not be forced to invent some.
    """
    resp = session().get(url, timeout=timeout)
    for _ in range(_LOGIN_ATTEMPTS):
        spent = looks_exhausted(resp)
        if spent:
            # Nothing to retry. Say it once, at a level an operator will see, and hand
            # the response back so the caller can report the real reason.
            log.warning("gateway refused: %s", spent)
            break
        if not looks_like_login(resp) or not login(settings, timeout):
            break
        resp = session().get(url, timeout=timeout)
    return resp
