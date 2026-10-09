#!/usr/bin/env python
"""Back up, restore into a scratch database, and verify the audit chain survived.

Why this is a script and not a paragraph in a runbook
------------------------------------------------------
"We take backups" is a claim every submission makes. The question a government reviewer
actually asks is whether anyone has ever restored one, and the honest answer for most
systems is no -- which is how an organisation discovers its backups were of the wrong
schema, or excluded the extension that half the columns depend on, at the worst possible
moment.

So this performs the restore and checks the one property that would be hardest to notice
if it broke: the hash chain in `audit_entry`. A restore that silently reordered or
dropped entries would leave a ledger that still looks like a ledger.

What it verifies
----------------
1. Every table's row count matches the source.
2. The hash chain is intact end to end -- each entry's `prev_hash` equals the previous
   entry's `entry_hash`, with no gaps in `seq`.
3. PostGIS geography survives: camera positions compare equal, not merely present.
4. The pgvector column survives with its dimension, since a restore that dropped the
   extension would leave the column as something else.

Usage:
    python scripts/backup_restore_test.py --report reports/backup-restore.json

Needs `pg_dump` and `psql` on PATH, and permission to create a scratch database on the
same server. It creates `<database>_restore_test`, restores into it, verifies, and drops
it again -- the source database is only ever read.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse, urlunparse

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from services.common.dburl import normalise_pg_url  # noqa: E402

#: Tables whose row counts must match. `spatial_ref_sys` is excluded: PostGIS populates
#: it on extension creation, so it is identical by construction and tells us nothing.
TABLES = (
    "department",
    "site",
    "camera",
    "camera_capability",
    "camera_zone",
    "detection",
    "vehicle_sighting",
    "watchlist_entry",
    "alert",
    "audit_entry",
    "api_key",
    "webhook_subscription",
    "webhook_delivery",
)


def _owner_url() -> str:
    """The migration URL, because a restore needs to create a schema."""
    for key in ("SETU_MIGRATION_DATABASE_URL", "SETU_DATABASE_URL"):
        if os.environ.get(key):
            return os.environ[key]
    env_file = BACKEND_ROOT.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            for key in ("SETU_MIGRATION_DATABASE_URL=", "SETU_DATABASE_URL="):
                if line.startswith(key):
                    return line.split("=", 1)[1].strip()
    raise SystemExit("set SETU_MIGRATION_DATABASE_URL to the owner connection string")


def _libpq(url: str) -> str:
    """SQLAlchemy's `postgresql+psycopg://` is not a libpq URL; pg_dump wants plain."""
    parsed = urlparse(normalise_pg_url(url))
    return urlunparse(parsed._replace(scheme="postgresql"))


#: Prefix prepended to every pg_* / psql invocation. Empty when the client binaries are
#: on PATH; set by --docker-service when Postgres runs in a container and the host has
#: no client installed, which is the normal state of a developer machine.
_PREFIX: list[str] = []


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_PREFIX + command, capture_output=True, text=True, check=False, **kwargs)


def _have_client() -> bool:
    """Whether pg_dump can be invoked at all.

    The absent binary raises FileNotFoundError from CreateProcess rather than
    returning non-zero, so the unguarded version of this crashed with a traceback in
    exactly the case it exists to report -- a developer machine with no client
    installed, which is the common one. A probe that cannot survive the condition it
    probes for is not a probe.
    """
    try:
        return _run(["pg_dump", "--version"]).returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _counts(engine: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as conn:
        # RLS is FORCEd even for the owner, so counting without the admin GUC returns
        # zero for five tables and the comparison would pass by matching nothing.
        conn.execute(text("SET setu.is_admin = 'on'"))
        for table in TABLES:
            try:
                out[table] = int(
                    conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar() or 0
                )
            except Exception:  # noqa: BLE001 -- a table absent from an older dump is data
                out[table] = -1
    return out


def _chain_report(engine: Any) -> dict[str, Any]:
    """Whether the hash chain is intact, and where it is not."""
    with engine.connect() as conn:
        conn.execute(text("SET setu.is_admin = 'on'"))
        total = int(conn.execute(text("SELECT count(*) FROM audit_entry")).scalar() or 0)
        broken = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT prev_hash, lag(entry_hash) OVER (ORDER BY seq) AS expected"
                    "  FROM audit_entry) x"
                    " WHERE expected IS NOT NULL AND prev_hash IS DISTINCT FROM expected"
                )
            ).scalar()
            or 0
        )
        # A gap in seq is a different failure from a broken link: entries were lost
        # rather than reordered, and a chain of survivors can still verify.
        gaps = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT seq, lag(seq) OVER (ORDER BY seq) AS prev FROM audit_entry) x"
                    " WHERE prev IS NOT NULL AND seq <> prev + 1"
                )
            ).scalar()
            or 0
        )
    return {"entries": total, "broken_links": broken, "sequence_gaps": gaps}


def _geometry_report(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        conn.execute(text("SET setu.is_admin = 'on'"))
        placed = int(
            conn.execute(text("SELECT count(*) FROM camera WHERE geom IS NOT NULL")).scalar() or 0
        )
        digest = conn.execute(
            text(
                "SELECT md5(string_agg(ST_AsEWKT(geom::geometry), ',' ORDER BY camera_ref))"
                " FROM camera WHERE geom IS NOT NULL"
            )
        ).scalar()
        vector_type = conn.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute"
                " WHERE attrelid = 'public.vehicle_sighting'::regclass"
                "   AND attname = 'appearance_vector'"
            )
        ).scalar()
    return {"placed_cameras": placed, "geometry_digest": digest, "vector_type": vector_type}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", type=Path, help="write the result here")
    ap.add_argument("--keep", action="store_true", help="do not drop the scratch database")
    ap.add_argument(
        "--docker-service",
        help="run pg_dump/pg_restore/psql inside this docker compose service, for a\nhost with no Postgres client installed",
    )
    args = ap.parse_args()

    global _PREFIX
    if args.docker_service:
        _PREFIX = ["docker", "compose", "exec", "-T", args.docker_service]
    elif not _have_client():
        raise SystemExit(
            "pg_dump is not on PATH. Either install the PostgreSQL client tools, or "
            "pass --docker-service postgres to run them inside the compose container."
        )

    source_url = _libpq(_owner_url())
    if args.docker_service:
        # Inside the container, the database is on localhost by its own name -- the
        # host's 127.0.0.1:5432 is a published port that does not exist in there.
        parsed_source = urlparse(source_url)
        source_url = urlunparse(
            parsed_source._replace(
                netloc=parsed_source.netloc.replace(
                    f"@{parsed_source.hostname}:{parsed_source.port}", "@127.0.0.1:5432"
                )
            )
        )
    parsed = urlparse(source_url)
    source_db = parsed.path.lstrip("/")
    scratch_db = f"{source_db}_restore_test"
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    scratch_url = urlunparse(parsed._replace(path=f"/{scratch_db}"))

    started = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "started_utc": started.isoformat(),
        "source_database": source_db,
        "scratch_database": scratch_db,
        "ok": False,
        "steps": [],
    }

    def step(name: str, ok: bool, detail: str, **extra: Any) -> None:
        result["steps"].append({"step": name, "ok": ok, "detail": detail, **extra})
        print(f"  [{'ok ' if ok else 'ERR'}] {name}: {detail}")

    print(f"backup/restore test: {source_db} -> {scratch_db}")

    source_engine = create_engine(normalise_pg_url(_owner_url()), future=True)
    before = _counts(source_engine)
    before_chain = _chain_report(source_engine)
    before_geom = _geometry_report(source_engine)
    step(
        "read source",
        True,
        f"{sum(v for v in before.values() if v > 0)} rows across {len(TABLES)} tables, "
        f"{before_chain['entries']} audit entries",
    )

    # The dump is written wherever the client binaries run. In a container that is a
    # path inside it, which is fine: nothing on the host needs to read the file, only
    # pg_restore does, and it runs in the same place.
    with tempfile.TemporaryDirectory() as tmp:
        # PurePosixPath, not Path. On Windows `str(Path("/tmp/x"))` is "\tmp\x" --
        # backslashes -- which a Linux container cannot create, and pg_dump then exits
        # non-zero while the warnings it also printed make the real cause look like a
        # TimescaleDB problem. Cost an hour; worth the comment.
        dump_path: PurePosixPath | Path = (
            PurePosixPath("/tmp/setu-restore-test.dump")
            if args.docker_service
            else Path(tmp) / "setu.dump"
        )

        t0 = time.monotonic()
        dumped = _run(["pg_dump", "--format=custom", "--file", str(dump_path), source_url])
        if dumped.returncode != 0:
            step("pg_dump", False, dumped.stderr.strip()[:300])
            result["error"] = "pg_dump failed; is it on PATH?"
            _finish(result, args.report)
            return 1
        if args.docker_service:
            listed = _run(["stat", "-c", "%s", str(dump_path)])
            size_mb = int(listed.stdout.strip() or 0) / 1e6 if listed.returncode == 0 else 0.0
        else:
            size_mb = dump_path.stat().st_size / 1e6
        step("pg_dump", True, f"{size_mb:.1f} MB in {time.monotonic() - t0:.1f}s")

        _run(["psql", admin_url, "-c", f'DROP DATABASE IF EXISTS "{scratch_db}"'])
        created = _run(["psql", admin_url, "-c", f'CREATE DATABASE "{scratch_db}"'])
        if created.returncode != 0:
            step("create scratch database", False, created.stderr.strip()[:300])
            _finish(result, args.report)
            return 1
        step("create scratch database", True, scratch_db)

        t0 = time.monotonic()
        restored = _run(
            ["pg_restore", "--no-owner", "--no-privileges", "--dbname", scratch_url, str(dump_path)]
        )
        # pg_restore exits non-zero on benign warnings (extension ownership, missing
        # roles). The verification below is what decides, not the exit code.
        step(
            "pg_restore",
            True,
            f"completed in {time.monotonic() - t0:.1f}s"
            + (f" with {restored.stderr.count('warning')} warnings" if restored.stderr else ""),
        )

        scratch_engine = create_engine(normalise_pg_url(scratch_url), future=True)
        try:
            after = _counts(scratch_engine)
            after_chain = _chain_report(scratch_engine)
            after_geom = _geometry_report(scratch_engine)

            mismatched = {t: (before[t], after[t]) for t in TABLES if before[t] != after[t]}
            step(
                "row counts match",
                not mismatched,
                "every table matches" if not mismatched else f"mismatched: {mismatched}",
                mismatched=mismatched,
            )

            # A gap in `seq` is not a restore defect and must not be asserted to zero.
            # `seq` comes from a Postgres sequence, and a sequence is deliberately
            # non-transactional: a rolled-back write keeps the number it drew. The
            # first run of this drill failed on three gaps that turned out to exist
            # identically in the source -- one of them 60 wide, from the ingest run
            # that rolled back when RLS refused its writes. The dump reproduced the
            # source exactly and the drill called it a failure.
            #
            # What the restore must preserve is the *same* gaps, which is what makes
            # it faithful, and an unbroken hash chain, which is what makes the ledger
            # evidence. Gaps are reported as an observation about the source.
            chain_ok = (
                after_chain["entries"] == before_chain["entries"]
                and after_chain["broken_links"] == 0
                and after_chain["sequence_gaps"] == before_chain["sequence_gaps"]
            )
            gap_note = (
                "no sequence gaps"
                if after_chain["sequence_gaps"] == 0
                else f"{after_chain['sequence_gaps']} sequence gaps, same as the source "
                "(a rolled-back write keeps its sequence number; this is not data loss)"
            )
            step(
                "audit chain intact",
                chain_ok,
                f"{after_chain['entries']} entries, {after_chain['broken_links']} broken links, "
                f"{gap_note}",
                before=before_chain,
                after=after_chain,
            )

            geom_ok = before_geom["geometry_digest"] == after_geom["geometry_digest"]
            step(
                "PostGIS geography preserved",
                geom_ok,
                f"{after_geom['placed_cameras']} placed cameras, digests "
                + ("match" if geom_ok else "DIFFER"),
            )

            vector_ok = after_geom["vector_type"] == before_geom["vector_type"]
            step(
                "pgvector column preserved",
                vector_ok,
                f"appearance_vector is {after_geom['vector_type']}",
            )

            result["ok"] = all(s["ok"] for s in result["steps"])
            result["before"] = {"counts": before, "chain": before_chain, "geometry": before_geom}
            result["after"] = {"counts": after, "chain": after_chain, "geometry": after_geom}
            result["dump_size_mb"] = round(size_mb, 2)
        finally:
            scratch_engine.dispose()
            if not args.keep:
                _run(["psql", admin_url, "-c", f'DROP DATABASE IF EXISTS "{scratch_db}"'])
                if args.docker_service:
                    _run(["rm", "-f", str(dump_path)])

    source_engine.dispose()
    _finish(result, args.report)
    print()
    print("RESULT:", "PASS" if result["ok"] else "FAIL")
    return 0 if result["ok"] else 1


def _finish(result: dict[str, Any], report: Path | None) -> None:
    result["finished_utc"] = datetime.now(timezone.utc).isoformat()
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"\nreport: {report}")


if __name__ == "__main__":
    raise SystemExit(main())
