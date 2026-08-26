#!/usr/bin/env python
"""Produce the submission's required output report: vehicles, plates, timestamps.

The challenge asks for a report of detected vehicles and number plates with their
corresponding timestamps. This produces it as both CSV (for a reviewer who wants to
sort and filter) and PDF (for the submission bundle).

Two things it deliberately does that a naive export would not:

**It reports unparsed reads too.** A detection whose text did not match Indian plate
grammar is still evidence that a vehicle passed a camera at a time. Dropping those
rows would make the report look cleaner and make it less true.

**It states the provenance of every row.** Which camera, whether that camera is a live
government feed or our own-feed replay, how many characters were corrected, and the
confidence. A reviewer can tell at a glance which rows are strong and which are leads.

Usage:
    python scripts/detection_report.py [--emit-evidence] [--source gateway|file|all]
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from services.api.db import get_sessionmaker  # noqa: E402
from services.api.tenancy import set_admin_context  # noqa: E402
from services.common import evidence, redact  # noqa: E402
from services.common.paths import REPORTS_DIR  # noqa: E402

log = logging.getLogger("report")

COLUMNS = [
    "observed_at_utc", "camera_ref", "camera_name", "location",
    "plate", "grammar_valid", "corrections", "confidence",
    "stream_pts_ms", "ingested_at_utc", "clock_confidence",
    "source_type", "evidence_crop",
]


def _rows(session, source: str) -> list[dict]:
    clause = ""
    if source == "gateway":
        clause = "AND c.source_type = 'gateway'"
    elif source == "file":
        clause = "AND c.source_type = 'file'"

    return [
        dict(r)
        for r in session.execute(
            text(
                f"""
                SELECT d.observed_at_utc, d.plate_normalised, d.plate_raw,
                       d.corrections, d.confidence, d.pts_ms, d.ingested_at_utc,
                       d.clock_confidence, d.crop_path,
                       c.camera_ref, c.name AS camera_name, c.location_text,
                       c.source_type
                FROM detection d
                JOIN camera c ON c.id = d.camera_id
                WHERE 1 = 1 {clause}
                ORDER BY d.observed_at_utc, c.camera_ref
                """
            )
        ).mappings()
    ]


_PLATE_RE = None


def _grammar_valid(plate: str) -> bool:
    global _PLATE_RE
    if _PLATE_RE is None:
        from services.analytics.plate_grammar import PLATE_RE

        _PLATE_RE = PLATE_RE
    return bool(_PLATE_RE.match(plate or ""))


def _to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "observed_at_utc": r["observed_at_utc"].isoformat(),
            "camera_ref": r["camera_ref"],
            "camera_name": r["camera_name"],
            "location": r["location_text"] or "",
            "plate": r["plate_normalised"],
            "grammar_valid": "yes" if _grammar_valid(r["plate_normalised"]) else "no",
            "corrections": len(r["corrections"] or []),
            "confidence": f"{float(r['confidence'] or 0):.3f}",
            "stream_pts_ms": f"{float(r['pts_ms'] or 0):.0f}",
            "ingested_at_utc": r["ingested_at_utc"].isoformat() if r["ingested_at_utc"] else "",
            "clock_confidence": f"{float(r['clock_confidence'] or 0):.2f}",
            "source_type": r["source_type"],
            "evidence_crop": Path(r["crop_path"]).name if r["crop_path"] else "",
        })
    return buf.getvalue()


def _to_pdf(rows: list[dict], gateway_note: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title="SETU detected vehicles and number plates",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=styles["BodyText"], fontSize=8, leading=11)
    small = ParagraphStyle("s", parent=body, fontSize=7, textColor=colors.HexColor("#555555"))

    valid = [r for r in rows if _grammar_valid(r["plate_normalised"])]
    cameras = {r["camera_ref"] for r in rows}
    distinct = {r["plate_normalised"] for r in valid}

    story = [
        Paragraph("Detected vehicles and number plates", styles["Heading1"]),
        Paragraph("Project SETU — Gujarat Police CCTV Integration Platform", small),
        Spacer(1, 6),
        Paragraph(
            f"<b>{len(rows)}</b> detections across <b>{len(cameras)}</b> cameras. "
            f"<b>{len(valid)}</b> parse as Indian registrations, covering "
            f"<b>{len(distinct)}</b> distinct plates. "
            f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}.",
            body,
        ),
        Spacer(1, 4),
        Paragraph(gateway_note, small),
        Spacer(1, 4),
        Paragraph(
            "Rows whose text did not match plate grammar are included. A detection that "
            "could not be parsed is still evidence that a vehicle passed a camera at a "
            "time, and omitting those rows would make this report look cleaner and be "
            "less true.", small,
        ),
        Spacer(1, 8),
    ]

    header = ["Observed (UTC)", "Camera", "Location", "Plate", "Valid",
              "Corr", "Conf", "PTS ms", "Source"]
    data = [header]
    for r in rows:
        data.append([
            r["observed_at_utc"].strftime("%Y-%m-%d %H:%M:%S"),
            r["camera_ref"],
            (r["location_text"] or "")[:26],
            r["plate_normalised"],
            "yes" if _grammar_valid(r["plate_normalised"]) else "no",
            str(len(r["corrections"] or [])),
            f"{float(r['confidence'] or 0):.2f}",
            f"{float(r['pts_ms'] or 0):.0f}",
            r["source_type"],
        ])

    table = Table(data, repeatRows=1, colWidths=[
        34 * mm, 24 * mm, 48 * mm, 26 * mm, 12 * mm, 12 * mm, 14 * mm, 18 * mm, 20 * mm,
    ])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
        ("FONTNAME", (3, 1), (3, -1), "Courier"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(table)
    doc.build(story)
    return buffer.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=("gateway", "file", "all"), default="all")
    ap.add_argument("--emit-evidence", action="store_true")
    args = ap.parse_args()
    redact.install(level=logging.INFO)

    session = get_sessionmaker()()
    set_admin_context(session)
    try:
        rows = _rows(session, args.source)
        gateway_rows = [r for r in rows if r["source_type"] == "gateway"]
    finally:
        session.close()

    if not rows:
        log.error("no detections; run the pipeline first")
        return 2

    if not gateway_rows:
        gateway_note = (
            "<b>Gateway status:</b> the Government-provided feed at live.corp8.cloud "
            "returned HTTP 502 on every media playlist throughout the build, so no "
            "detections in this report originate from it. Its catalogue endpoint "
            "remained reachable. The fault was reported to the organisers; see "
            "docs/SUPPORT_QUERY.md. Every row here comes from our own-feed footage "
            "processed through the identical pipeline."
        )
    else:
        gateway_note = (
            f"<b>Gateway status:</b> {len(gateway_rows)} of {len(rows)} detections "
            "originate from the Government-provided feed."
        )

    csv_text = _to_csv(rows)
    pdf_bytes = _to_pdf(rows, gateway_note)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    csv_path = REPORTS_DIR / f"detections-{stamp}.csv"
    pdf_path = REPORTS_DIR / f"detections-{stamp}.pdf"
    csv_path.write_text(csv_text, encoding="utf-8")
    pdf_path.write_bytes(pdf_bytes)

    valid = [r for r in rows if _grammar_valid(r["plate_normalised"])]
    print("\nDetected vehicles and number plates")
    print(f"  detections        : {len(rows)}")
    print(f"  cameras           : {len({r['camera_ref'] for r in rows})}")
    print(f"  grammar-valid     : {len(valid)}")
    print(f"  distinct plates   : {len({r['plate_normalised'] for r in valid})}")
    print(f"  from gateway feed : {len(gateway_rows)}")
    print(f"\n  {csv_path}")
    print(f"  {pdf_path}\n")

    if args.emit_evidence:
        payload = {
            "detections": len(rows),
            "cameras": len({r["camera_ref"] for r in rows}),
            "grammar_valid": len(valid),
            "distinct_plates": len({r["plate_normalised"] for r in valid}),
            "from_gateway": len(gateway_rows),
            "gateway_note": gateway_note.replace("<b>", "").replace("</b>", ""),
            "csv_file": csv_path.name,
            "pdf_file": pdf_path.name,
            "rows": [
                {
                    "observed_at_utc": r["observed_at_utc"].isoformat(),
                    "camera_ref": r["camera_ref"],
                    "plate": r["plate_normalised"],
                    "grammar_valid": _grammar_valid(r["plate_normalised"]),
                    "corrections": len(r["corrections"] or []),
                    "confidence": round(float(r["confidence"] or 0), 3),
                    "pts_ms": round(float(r["pts_ms"] or 0)),
                    "source_type": r["source_type"],
                }
                for r in rows
            ],
        }
        md = [
            "# Detected vehicles and number plates", "",
            payload["gateway_note"], "",
            "| metric | value |", "|---|---:|",
            f"| Detections | {len(rows)} |",
            f"| Cameras | {payload['cameras']} |",
            f"| Parse as Indian registrations | {len(valid)} |",
            f"| Distinct plates | {payload['distinct_plates']} |",
            f"| From the Government feed | {len(gateway_rows)} |",
            "",
            "Full rows in the accompanying CSV and PDF. Unparsed reads are included: a",
            "detection that did not match plate grammar is still evidence a vehicle",
            "passed a camera at a time.", "",
        ]
        j, m = evidence.write("detection-report", payload, "\n".join(md) + "\n")
        print(f"evidence: {j.name}, {m.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
