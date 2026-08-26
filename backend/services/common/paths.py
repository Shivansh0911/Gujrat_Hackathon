"""Canonical filesystem locations.

Every module previously computed its own `Path(__file__).parents[N]`, which is a
silent trap: the correct N depends on how deep the file sits, so moving a file one
directory changes a constant that nothing verifies. During the split into
`backend/` and `frontend/` several of those constants pointed at the wrong tree and
the failures surfaced far from the cause -- evidence written into `backend/reports`,
crops served from a directory that did not exist.

Defining the roots once, relative to this file, means a future move breaks in exactly
one place and is fixed in exactly one place.

Layout:

    <project>/                  PROJECT_ROOT   .env, docker-compose.yml, Makefile
      backend/                  BACKEND_ROOT   services, migrations, scripts, tests
        services/common/paths.py
      frontend/                                the React console
      data/                     DATA_DIR       seeds, own-feed footage, evidence
      reports/                  REPORTS_DIR    run reports and committed evidence
      docs/                     DOCS_DIR
"""

from __future__ import annotations

from pathlib import Path

# .../backend/services/common/paths.py -> parents[2] is backend/
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Path = BACKEND_ROOT.parent

# Shared trees deliberately live above `backend/`: the footage, seeds and evidence are
# project artefacts, not Python package data, and the frontend serves crops from them.
DATA_DIR: Path = PROJECT_ROOT / "data"
REPORTS_DIR: Path = PROJECT_ROOT / "reports"
DOCS_DIR: Path = PROJECT_ROOT / "docs"

EVIDENCE_DIR: Path = REPORTS_DIR / "evidence"
CROPS_DIR: Path = DATA_DIR / "evidence" / "crops"
SEED_DIR: Path = DATA_DIR / "seed"
OWN_FEED_DIR: Path = DATA_DIR / "own_feed"

# Configuration is read from the project root, not the working directory: `make demo`,
# pytest and uvicorn are each launched from a different place, and a relative .env
# resolves differently in all three.
ENV_FILE: Path = PROJECT_ROOT / ".env"

__all__ = [
    "BACKEND_ROOT", "PROJECT_ROOT", "DATA_DIR", "REPORTS_DIR", "DOCS_DIR",
    "EVIDENCE_DIR", "CROPS_DIR", "SEED_DIR", "OWN_FEED_DIR", "ENV_FILE",
]
