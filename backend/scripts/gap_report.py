#!/usr/bin/env python
"""Sample gap-analysis report — where the estate cannot see, and what each gap costs.

Model 1 asks for this as its own artefact, separate from the detection report. The
two answer opposite questions: the detection report says what the cameras *did* see,
this says where they cannot.

The structure follows the API's own grouping rather than inventing one, because the
separation is the whole point. A gap is sorted by **remedy**, not by severity, and the
remedies differ by orders of magnitude in cost:

  * a missing coordinate is somebody walking to the camera with a phone
  * an approximate coordinate needs a survey
  * a degraded camera needs maintenance on capital already spent
  * uncovered ground needs procurement

Ranking them all on one "severity" scale would put a five-minute fix next to a
five-lakh one and make the list useless for planning, which is what it is for.

The last section is the one worth reading: positions that real plate queries kept
needing and where nothing was watching. That is an evidence-backed argument for where
the next camera goes, derived from investigations that actually happened rather than
from a coverage model.

    python scripts/gap_report.py --emit-evidence
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common import evidence, redact  # noqa: E402
from services.common.paths import REPORTS_DIR  # noqa: E402


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def build_markdown(data: dict, generated: datetime) -> str:
    lines: list[str] = []
    A = lines.append

    A("# Gap analysis — where the estate cannot see")
    A("")
    A(f"Generated {generated.isoformat(timespec='seconds')} from the live registry.")
    A("")
    A(
        "Gaps are grouped by **remedy**, not by severity. A missing coordinate is a "
        "pin drop; uncovered ground is a procurement. Ranking both on one scale would "
        "put a five-minute fix beside a capital purchase and make the list useless for "
        "the planning it exists to support."
    )
    A("")

    districts = data.get("districts", [])
    camera_gaps = data.get("camera_gaps", [])
    journey_gaps = data.get("journey_gaps", [])

    # ---------------------------------------------------------------- summary
    A("## Summary")
    A("")
    A("| | |")
    A("|---|---:|")
    A(f"| Districts assessed | {len(districts)} |")
    A(f"| Cameras with a gap | {len(camera_gaps)} |")
    A(f"| Investigation-derived gaps | {len(journey_gaps)} |")
    A("")

    by_kind: dict[str, int] = {}
    for g in camera_gaps:
        by_kind[g["kind"]] = by_kind.get(g["kind"], 0) + 1

    if by_kind:
        A("### By remedy")
        A("")
        A("| Kind | Count | What fixing it takes |")
        A("|---|---:|---|")
        REMEDY = {
            "no_coordinate": "An operator drops a pin. Minutes.",
            "low_confidence": "A survey visit to establish the real position.",
            "degraded": "Maintenance on capital already spent.",
            "unreachable": "Network or vendor investigation; the camera exists.",
        }
        for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            A(f"| `{kind}` | {n} | {REMEDY.get(kind, '—')} |")
        A("")

    # -------------------------------------------------------------- districts
    if districts:
        A("## Coverage by district")
        A("")
        A(
            "Confidence is a property of what we know about the cameras, not a claim "
            "about the roads. A district of cameras placed only to a centroid scores "
            "low because their positions are uncertain, not because the area is unwatched."
        )
        A("")
        A("| District | Cameras | Placed | Confidence | Findings |")
        A("|---|---:|---:|---:|---|")
        for d in sorted(districts, key=lambda x: x.get("coverage_confidence", 0)):
            A(
                f"| {d['district']} | {d.get('cameras_total', 0)} | "
                f"{d.get('cameras_placed', 0)} | "
                f"{_fmt_pct(d.get('coverage_confidence', 0))} | "
                f"{'; '.join(d.get('findings', [])) or '—'} |"
            )
        A("")

    # ------------------------------------------------------------ camera gaps
    if camera_gaps:
        A("## Cameras with a gap")
        A("")
        A("| Camera | Kind | Detail |")
        A("|---|---|---|")
        for g in camera_gaps:
            A(f"| {g.get('camera_ref', '?')} | `{g['kind']}` | {g.get('detail', '')} |")
        A("")

    # ----------------------------------------------------------- journey gaps
    A("## Investigation-derived gaps")
    A("")
    if journey_gaps:
        A(
            "Positions that real plate queries kept needing, where nothing was "
            "watching. This is the evidence-backed case for where the next camera "
            "should go — derived from investigations that happened, not from a model."
        )
        A("")
        A("| Location | Times needed | Plates affected |")
        A("|---|---:|---|")
        for g in journey_gaps:
            A(
                f"| {g.get('name', '?')} | {g.get('times_needed', 0)} | "
                f"{', '.join(g.get('plates', [])) or '—'} |"
            )
    else:
        A(
            "None yet. This section fills as journey queries are run: a gap appears "
            "here when a route reconstruction repeatedly needed a position no camera "
            "covers. An empty section means the queries run so far were all served by "
            "existing coverage, not that no gaps exist."
        )
    A("")

    A("---")
    A("")
    A(
        "Produced by `backend/scripts/gap_report.py` from `GET /cameras/gap-analysis`, "
        "the same computation the Coverage screen renders. No figure here is entered "
        "by hand."
    )
    A("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--emit-evidence", action="store_true", help="also write a dated evidence record"
    )
    ap.add_argument(
        "--days", type=int, default=7, help="window for investigation-derived gaps (1-90)"
    )
    args = ap.parse_args()

    redact.install()

    from services.api.db import get_sessionmaker
    from services.api.routers.gaps import gap_analysis
    from services.api.security import Actor
    from services.api.tenancy import set_admin_context

    session = get_sessionmaker()()
    set_admin_context(session)
    try:
        # A report spans the estate, so it runs with the same admin context the seed
        # and matcher use. Department scoping is a property of an operator's session,
        # not of an estate-wide planning document.
        actor = Actor(subject="gap_report", role="admin", department_id=None)
        result = gap_analysis(session=session, actor=actor, journey_window_days=args.days)
        data = result.model_dump(mode="json")
    finally:
        session.close()

    generated = datetime.now(timezone.utc)
    markdown = build_markdown(data, generated)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = generated.strftime("%Y-%m-%dT%H-%M-%SZ")
    out = REPORTS_DIR / f"gap-analysis-{stamp}.md"
    out.write_text(markdown, encoding="utf-8")

    print(f"\n  districts assessed        : {len(data.get('districts', []))}")
    print(f"  cameras with a gap        : {len(data.get('camera_gaps', []))}")
    print(f"  investigation-derived     : {len(data.get('journey_gaps', []))}")
    print(f"\nreport: {out}")

    if args.emit_evidence:
        j, m = evidence.write("gap-analysis", data, markdown)
        print(f"evidence: {j.name} and {m.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
