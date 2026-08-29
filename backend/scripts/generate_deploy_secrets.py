#!/usr/bin/env python
"""Generate the secrets a deployment needs, into a gitignored file.

Every value is generated with `secrets`, never chosen. Two of them --
`SETU_ADMIN_PASSWORD` and `SETU_OPERATOR_PASSWORD` -- are the test credentials that go
on the submission form and into `docs/DEMO_RUNBOOK.md` §1, so they need to be
retrievable rather than scrolled past in a terminal.

The output path is in `.gitignore`. This script refuses to overwrite an existing file
without `--force`, because regenerating after a deployment is live silently
invalidates the credentials someone has already written down.
"""

from __future__ import annotations

import argparse
import datetime
import secrets
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import PROJECT_ROOT  # noqa: E402

OUT = PROJECT_ROOT / "deploy-secrets.env"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args()

    if OUT.exists() and not args.force:
        print(f"{OUT.name} already exists. Refusing to overwrite.")
        print("If a deployment is already using these, regenerating breaks it.")
        print("Pass --force if you are sure.")
        return 1

    body = "\n".join(
        [
            f"# Deployment secrets. Generated {datetime.date.today().isoformat()}.",
            "# Gitignored: never commit this file.",
            "#",
            "# SETU_ADMIN_PASSWORD and SETU_OPERATOR_PASSWORD are the test credentials",
            "# the screening committee uses. They go on the submission form and into",
            "# docs/DEMO_RUNBOOK.md section 1 once the deployment is live.",
            "",
            f"SETU_JWT_SECRET={secrets.token_urlsafe(48)}",
            f"SETU_APP_DB_PASSWORD={secrets.token_urlsafe(24)}",
            f"SETU_ADMIN_PASSWORD={secrets.token_urlsafe(18)}",
            f"SETU_OPERATOR_PASSWORD={secrets.token_urlsafe(18)}",
            f"SETU_EVIDENCE_SIGNING_KEY={secrets.token_hex(32)}",
            "",
            "# Set once the services exist:",
            "#   SETU_CORS_ORIGINS           the exact console origin, no trailing slash",
            "#   VITE_API_ORIGIN             the backend public origin, no /api suffix",
            "#   SETU_MIGRATION_DATABASE_URL the platform's own database URL",
            "",
        ]
    )
    OUT.write_text(body, encoding="utf-8")
    print(f"wrote {OUT}")
    print("Keep SETU_ADMIN_PASSWORD and SETU_OPERATOR_PASSWORD: they go on the form.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
