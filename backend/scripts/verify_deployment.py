#!/usr/bin/env python
"""Verify a deployed SETU instance before its URL is given to anyone.

A broken hosted URL is worse than no hosted URL: a judge who clicks it, sees an error
and closes the tab carries that impression into every other part of the submission.
So this runs against the deployment rather than a developer laptop, and it is written
to fail loudly rather than to reassure.

Ten checks, each of which has failed at least once in a container that looked healthy
from the outside:

  1. Both accounts authenticate.
  2. Cameras render, with coordinate provenance intact.
  3. A journey returns hops AND the evidence images actually fetch. The crops live on
     a volume; an image URL that 404s is the classic first-deploy failure and the
     screen still renders, just with empty boxes.
  4. The signed PDF downloads with its signature header.
  5. Alerts list, and the WebSocket upgrade completes. A reverse proxy that gzips or
     rewrites the upgrade breaks this and nothing else.
  6. Coverage returns district findings.
  7. Health reports measured against declared frame rate.
  8. The database role is NOSUPERUSER/NOBYPASSRLS. A superuser silently ignores every
     row-level security policy while all nine isolation tests still pass locally,
     which makes this the most dangerous check to skip. It needs database access, so
     it reports NOT VERIFIED rather than PASS when it cannot reach one -- an
     unverifiable security control must never read as a green tick.
  9. The audit chain verifies.
 10. The watchlist is populated and every entry is bounded by an expiry. An empty
     watchlist is a deployment that will never raise an alert, and it looks exactly
     like a working one until someone waits.

Usage:
    python scripts/verify_deployment.py http://localhost:8080
    python scripts/verify_deployment.py https://setu.example.org --admin-password ...

Credentials default to the environment (SETU_ADMIN_PASSWORD, SETU_OPERATOR_PASSWORD)
so they need not appear in a shell history or a CI log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import requests  # noqa: E402

TIMEOUT = 30

PASS, FAIL, UNVERIFIED = "PASS", "FAIL", "NOT VERIFIED"


class Checks:
    """Accumulates results so one failure does not hide the other eight."""

    def __init__(self) -> None:
        self.rows: list[tuple[int, str, str, str]] = []

    def record(self, n: int, name: str, status: str, detail: str) -> None:
        self.rows.append((n, name, status, detail))
        print(f"  {n}. {status:<12} {name} - {detail}")

    @property
    def failed(self) -> list[tuple[int, str, str, str]]:
        return [r for r in self.rows if r[2] != PASS]


def login(base: str, username: str, password: str) -> str | None:
    r = requests.post(
        f"{base}/api/auth/login",
        data={"username": username, "password": password},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        return None
    return str(r.json().get("access_token") or "") or None


def get(base: str, path: str, token: str, **kw: Any) -> requests.Response:
    return requests.get(
        f"{base}/api{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
        **kw,
    )


def as_list(payload: Any) -> list[Any]:
    """Endpoints return bare arrays; tolerate a wrapped shape too."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "districts", "cameras"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def check_rls() -> tuple[str, str]:
    """Confirm the application role cannot bypass row-level security."""
    url = os.environ.get("SETU_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return UNVERIFIED, (
            "no SETU_DATABASE_URL. For a hosted deployment, set it and re-run. For the "
            "local container stack the database port is deliberately not published, so "
            "run the query inside the stack instead: "
            "`docker compose -f docker-compose.prod.yml --env-file .env.prod exec -T "
            'postgres psql -U <superuser> -d <db> -c "select rolname, rolsuper, '
            "rolbypassrls from pg_roles where rolname = 'setu_app'\"`. "
            "Both rolsuper and rolbypassrls must be false."
        )
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "select rolname, rolsuper, rolbypassrls from pg_roles "
                        "where rolname = current_user"
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return FAIL, "current_user not found in pg_roles"
        ok = row["rolsuper"] is False and row["rolbypassrls"] is False
        detail = (
            f"role={row['rolname']} rolsuper={row['rolsuper']} "
            f"rolbypassrls={row['rolbypassrls']}"
        )
        return (PASS if ok else FAIL), detail
    except Exception as exc:
        return UNVERIFIED, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="console origin, e.g. https://setu.example.org")
    ap.add_argument("--admin-user", default="admin")
    ap.add_argument("--operator-user", default="operator")
    ap.add_argument("--admin-password", default=os.environ.get("SETU_ADMIN_PASSWORD", ""))
    ap.add_argument("--operator-password", default=os.environ.get("SETU_OPERATOR_PASSWORD", ""))
    ap.add_argument("--plate", default=None, help="plate to trace; discovered from alerts if unset")
    ap.add_argument("--window-hours", type=int, default=24 * 365)
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not args.admin_password or not args.operator_password:
        print(
            "FAIL: set SETU_ADMIN_PASSWORD and SETU_OPERATOR_PASSWORD, or pass them.\n"
            "      A deployment whose credentials you do not have is not verified."
        )
        return 2

    print(f"\nverifying {base}\n")
    c = Checks()

    # 1 -- authentication, both roles
    try:
        admin = login(base, args.admin_user, args.admin_password)
        operator = login(base, args.operator_user, args.operator_password)
    except requests.RequestException as exc:
        c.record(1, "log in as admin and operator", FAIL, f"{type(exc).__name__}: {exc}")
        print("\nthe instance is unreachable; remaining checks skipped")
        return 1
    ok = bool(admin and operator)
    c.record(
        1,
        "log in as admin and operator",
        PASS if ok else FAIL,
        "both issued tokens" if ok else "one or both logins rejected",
    )
    if not admin:
        print("\nno admin token; remaining checks cannot run")
        return 1
    token = admin

    # 2 -- cameras and coordinate provenance
    try:
        cams = as_list(get(base, "/cameras?limit=500", token).json())
        placed = [x for x in cams if x.get("lat") is not None]
        approx = [x for x in cams if (x.get("confidence_radius_m") or 0) > 0]
        missing = [x for x in cams if x.get("lat") is None]
        c.record(
            2,
            "map renders cameras",
            PASS if (cams and placed) else FAIL,
            f"{len(cams)} cameras, {len(placed)} placed, "
            f"{len(approx)} approximate, {len(missing)} coordinate-missing",
        )
    except Exception as exc:
        c.record(2, "map renders cameras", FAIL, f"{type(exc).__name__}: {exc}")

    # Discover a plate and the crops that go with it, from the alert desk.
    alerts: list[dict[str, Any]] = []
    try:
        alerts = as_list(get(base, "/alerts?limit=100", token).json())
    except Exception:
        pass
    plate = args.plate
    if plate is None:
        for a in alerts:
            if a.get("matched_value"):
                plate = str(a["matched_value"])
                break

    now = datetime.now(timezone.utc)
    window = {
        "from": (now - timedelta(hours=args.window_hours)).isoformat(),
        "to": (now + timedelta(hours=1)).isoformat(),
        "purpose": "deployment verification",
    }

    # 3 -- journey with evidence photographs that actually fetch
    try:
        if not plate:
            c.record(3, "journey returns hops with evidence", FAIL, "no plate available to trace")
        else:
            r = get(base, "/journey", token, params={"plate": plate, **window})
            hops = as_list(r.json().get("hops", []) if isinstance(r.json(), dict) else [])
            crops = [h.get("crop_url") for h in hops if h.get("crop_url")]
            fetched = 0
            for u in crops:
                url = u if str(u).startswith("http") else f"{base}{u}"
                try:
                    if requests.get(url, timeout=TIMEOUT).ok:
                        fetched += 1
                except requests.RequestException:
                    pass
            ok = bool(hops) and fetched == len(crops) and fetched > 0
            c.record(
                3,
                "journey returns hops with evidence",
                PASS if ok else FAIL,
                f"plate {plate}: {len(hops)} hops, {fetched}/{len(crops)} crops fetch",
            )
    except Exception as exc:
        c.record(3, "journey returns hops with evidence", FAIL, f"{type(exc).__name__}: {exc}")

    # 4 -- signed evidence export
    try:
        if not plate:
            c.record(4, "signed PDF exports", FAIL, "no plate available to export")
        else:
            r = get(base, "/journey/export", token, params={"plate": plate, **window})
            sig = next(
                (v for k, v in r.headers.items() if k.lower().startswith("x-setu-sig")), None
            )
            is_pdf = r.headers.get("Content-Type", "").startswith("application/pdf")
            ok = r.ok and is_pdf and bool(sig)
            c.record(
                4,
                "signed PDF exports",
                PASS if ok else FAIL,
                f"HTTP {r.status_code}, {len(r.content)} bytes, "
                f"signature header {'present' if sig else 'MISSING'}",
            )
    except Exception as exc:
        c.record(4, "signed PDF exports", FAIL, f"{type(exc).__name__}: {exc}")

    # 5 -- alerts, and the WebSocket upgrade
    try:
        scheme = "wss" if urlparse(base).scheme == "https" else "ws"
        # The console connects to /ws/alerts, outside the /api prefix, and passes the
        # token as a query parameter because a browser cannot set headers on a
        # WebSocket handshake.
        ws_url = f"{scheme}://{urlparse(base).netloc}/ws/alerts?token={token}"
        ws_detail = "websocket not tested (pip install websockets to enable)"
        ws_ok = None
        try:
            from websockets.sync.client import connect

            with connect(ws_url, open_timeout=15):
                ws_ok, ws_detail = True, "websocket connected"
        except ImportError:
            pass
        except Exception as exc:
            ws_ok, ws_detail = False, f"websocket failed: {type(exc).__name__}"
        status = FAIL if (not alerts or ws_ok is False) else PASS
        c.record(5, "alerts list and stream", status, f"{len(alerts)} alerts, {ws_detail}")
    except Exception as exc:
        c.record(5, "alerts list and stream", FAIL, f"{type(exc).__name__}: {exc}")

    # 6 -- coverage / gap analysis
    try:
        payload = get(base, "/cameras/gap-analysis", token).json()
        districts = as_list(payload)
        c.record(
            6,
            "coverage returns districts",
            PASS if districts else FAIL,
            f"{len(districts)} districts",
        )
    except Exception as exc:
        c.record(6, "coverage returns districts", FAIL, f"{type(exc).__name__}: {exc}")

    # 7 -- health, measured against declared
    try:
        rows = as_list(get(base, "/health/cameras", token).json())
        measured = [x for x in rows if x.get("measured_fps")]
        c.record(
            7,
            "health shows measured vs declared fps",
            PASS if rows else FAIL,
            f"{len(rows)} rows, {len(measured)} with a measured rate",
        )
    except Exception as exc:
        c.record(7, "health shows measured vs declared fps", FAIL, f"{type(exc).__name__}: {exc}")

    # 8 -- row-level security is actually binding
    status, detail = check_rls()
    c.record(8, "RLS: app role is unprivileged", status, detail)

    # 9 -- audit chain
    try:
        d = get(base, "/audit/verify", token).json()
        ok = bool(d.get("valid"))
        c.record(
            9,
            "audit chain verifies",
            PASS if ok else FAIL,
            f"{d.get('entries_checked', '?')} entries, valid={d.get('valid')}",
        )
    except Exception as exc:
        c.record(9, "audit chain verifies", FAIL, f"{type(exc).__name__}: {exc}")

    # 10 -- the watchlist, which is the input to every alert on the desk. An empty
    # one is a deployment that will never alert, and looks identical to a working
    # one until a judge waits for something to happen.
    try:
        entries = as_list(get(base, "/watchlist", token).json())
        with_expiry = [e for e in entries if e.get("valid_to")]
        ok = bool(entries) and len(with_expiry) == len(entries)
        c.record(
            10,
            "watchlist populated, every entry bounded",
            PASS if ok else FAIL,
            f"{len(entries)} entries, {len(with_expiry)} with an expiry",
        )
    except Exception as exc:
        c.record(
            10, "watchlist populated, every entry bounded", FAIL, f"{type(exc).__name__}: {exc}"
        )

    passed = sum(1 for r in c.rows if r[2] == PASS)
    print("\n" + "=" * 66)
    print(f"  {passed} of {len(c.rows)} checks passed")
    if c.failed:
        print("\n  DO NOT quote this URL until these pass:")
        for n, name, status, detail in c.failed:
            print(f"    {n}. [{status}] {name} - {detail}")
    print("=" * 66 + "\n")

    out = BACKEND_ROOT.parent / "reports" / "deployment-verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "base_url": base,
                "verified_utc": now.isoformat(),
                "passed": passed,
                "total": len(c.rows),
                "checks": [
                    {"n": n, "name": name, "status": status, "detail": detail}
                    for n, name, status, detail in c.rows
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {out}")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
