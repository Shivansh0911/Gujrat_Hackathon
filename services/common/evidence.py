"""Dated, immutable evidence records for the submission bundle.

The competition scores "successful test case" and "submission completeness" on
artefacts, not assertions. An evidence file therefore records not only the result but
the exact conditions that produced it -- the gateway host, the git SHA of the code
that ran, whether the working tree was dirty, and the toolchain versions -- so a jury
can tell whether a claim is reproducible or merely stated.

Files are named by UTC timestamp and never overwritten: a re-run adds a record, it
does not replace one. That is what makes the directory a history rather than a claim
about the present.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "reports" / "evidence"


def _git(*args: str) -> str | None:
    """Run a git command, returning None rather than raising if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Evidence must still be produced on a machine without git (e.g. inside a
        # container); the provenance fields degrade to null rather than failing the run.
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def provenance() -> dict[str, Any]:
    """Conditions under which an evidence record was produced."""
    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    return {
        "git_sha": sha,
        # A dirty tree means the artefact does not correspond to any commit. Recording
        # it is the difference between evidence and an unverifiable screenshot.
        "git_tree_dirty": bool(dirty) if dirty is not None else None,
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


def timestamp() -> str:
    """UTC ISO-8601, filesystem-safe (colons are illegal in Windows filenames)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def write(kind: str, payload: dict[str, Any], markdown: str, ts: str | None = None) -> tuple[Path, Path]:
    """Write `<kind>-<utc>.json` and `<kind>-<utc>.md`. Returns both paths."""
    ts = ts or timestamp()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    record = {"kind": kind, "generated_at": ts, "provenance": provenance(), **payload}
    json_path = EVIDENCE_DIR / f"{kind}-{ts}.json"
    md_path = EVIDENCE_DIR / f"{kind}-{ts}.md"
    json_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path
