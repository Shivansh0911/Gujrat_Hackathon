"""Signed PDF export of the coverage gap analysis.

The Coverage screen answers a planning question -- where can this network not see, and
what would each blind spot cost to fix -- and a planning answer is only useful if it can
leave the screen. A superintendent deciding next year's procurement does not log into a
console; they read a document, forward it, and attach it to a proposal.

Why this is signed like the evidence export
-------------------------------------------
Because it makes the same kind of claim. The evidence export attests "this vehicle was
seen here"; this one attests "these 28 cameras have a stated defect and these positions
were needed by real investigations and covered by nothing". Both get quoted at people
who were not in the room, and both are worth being able to check. The manifest commits
to every figure printed, so a recipient with the public key can confirm the numbers were
not edited after leaving SETU.

What it deliberately does not do is invent a severity ranking. Gaps are grouped by
**remedy**, exactly as `GET /cameras/gap-analysis` groups them, because a missing
coordinate is somebody walking to a camera with a phone and uncovered ground is a
procurement -- putting both on one severity scale makes the document useless for the
planning it exists to support.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from services.api.evidence_export import _grid, _load_signing_key, public_key_hex

#: What fixing each kind of gap actually takes. The whole point of grouping by remedy is
#: lost if the document names the kinds without saying what they cost, so this text
#: travels with the table rather than living only in the reader's head.
REMEDY = {
    "no_coordinate": "An operator drops a pin. Minutes.",
    "low_confidence": "A survey visit to establish the real position.",
    "degraded": "Maintenance on capital already spent.",
    "unreachable": "Network or vendor investigation; the camera exists.",
}

KIND_LABEL = {
    "no_coordinate": "No coordinate",
    "low_confidence": "Low spatial confidence",
    "degraded": "Degraded",
    "unreachable": "Unreachable",
}


def build_manifest(data: dict[str, Any], audit_seq: int | None, generated: str) -> bytes:
    """Canonical JSON committing to every figure printed in the PDF.

    Sorted keys and tight separators, so two exports of the same analysis produce
    byte-identical manifests. A manifest whose bytes depend on dict ordering cannot be
    verified by anybody else, which would make signing it theatre.
    """
    payload = {
        "audit_seq": audit_seq,
        "generated_at": generated,
        "summary": data.get("summary", {}),
        "districts": [
            {
                "district": d["district"],
                "cameras_total": d["cameras_total"],
                "cameras_placed": d["cameras_placed"],
                "coverage_confidence": d["coverage_confidence"],
                "findings": d["findings"],
            }
            for d in data.get("districts", [])
        ],
        "camera_gaps": [
            {"camera_ref": g["camera_ref"], "kind": g["kind"], "detail": g["detail"]}
            for g in data.get("camera_gaps", [])
        ],
        "journey_gaps": [
            {"camera_ref": g["camera_ref"], "times_implied": g["times_implied"]}
            for g in data.get("journey_gaps", [])
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def render_pdf(
    data: dict[str, Any],
    *,
    audit_seq: int | None,
    requested_by: str,
    generated: str,
    manifest_sha256: str,
    signature_hex: str,
    public_key: str,
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title="SETU coverage gap analysis",
        author="Project SETU",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11.5)
    small = ParagraphStyle("small", parent=body, fontSize=7.2, textColor=colors.HexColor("#555555"))
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=6.5)

    summary: dict[str, Any] = data.get("summary", {})
    districts: list[dict[str, Any]] = data.get("districts", [])
    camera_gaps: list[dict[str, Any]] = data.get("camera_gaps", [])
    journey_gaps: list[dict[str, Any]] = data.get("journey_gaps", [])

    story: list[Any] = []
    story.append(Paragraph("Coverage gap analysis", h1))
    story.append(Paragraph("Project SETU — Gujarat Police CCTV Integration Platform", small))
    story.append(Spacer(1, 6))
    story.append(
        Paragraph(
            "Where this network cannot see, and what each blind spot would cost to close. "
            "Gaps are grouped by <b>remedy</b> rather than severity: a missing coordinate "
            "is a pin drop and uncovered ground is a procurement, and ranking both on one "
            "scale would put a five-minute fix beside a capital purchase.",
            body,
        )
    )
    story.append(Spacer(1, 6))

    header_rows = [
        ["Requested by", requested_by],
        ["Generated (UTC)", generated],
        ["Audit ledger entry", str(audit_seq) if audit_seq is not None else "not recorded"],
        ["Cameras assessed", str(summary.get("cameras_total", 0))],
        ["Districts assessed", str(summary.get("districts_covered", len(districts)))],
        ["Cameras with a gap", str(len(camera_gaps))],
        ["Investigation-derived gaps", str(len(journey_gaps))],
    ]
    table = Table(header_rows, colWidths=[42 * mm, 128 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#444444")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#DDDDDD")),
            ]
        )
    )
    story.append(table)

    # ---- by remedy ----
    by_kind: dict[str, int] = {}
    for g in camera_gaps:
        by_kind[g["kind"]] = by_kind.get(g["kind"], 0) + 1

    if by_kind:
        story.append(Paragraph("By remedy", h2))
        rows = [["Finding", "Cameras", "What fixing it takes"]] + [
            [KIND_LABEL.get(k, k), str(n), REMEDY.get(k, "—")]
            for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1])
        ]
        story.append(_grid(rows, [42 * mm, 18 * mm, 110 * mm]))

    # ---- districts ----
    if districts:
        story.append(Paragraph("Coverage confidence by district", h2))
        story.append(
            Paragraph(
                "Confidence is a property of what is known about the cameras, not a claim "
                "about the roads. A district whose cameras are placed only to a district "
                "centroid scores low because their positions are uncertain, not because "
                "the area is unwatched.",
                small,
            )
        )
        story.append(Spacer(1, 3))
        rows = [["District", "Cameras", "Placed", "Confidence", "Findings"]] + [
            [
                d["district"],
                str(d["cameras_total"]),
                str(d["cameras_placed"]),
                f"{d['coverage_confidence'] * 100:.0f}%",
                "; ".join(d.get("findings", [])) or "—",
            ]
            for d in sorted(districts, key=lambda x: x.get("coverage_confidence", 0))
        ]
        story.append(_grid(rows, [32 * mm, 16 * mm, 15 * mm, 20 * mm, 87 * mm]))

    # ---- investigation-derived gaps: the section worth reading ----
    story.append(Paragraph("Investigation-derived gaps", h2))
    if journey_gaps:
        story.append(
            Paragraph(
                "Positions that real plate queries kept needing, where nothing was seen. "
                "This is the evidence-backed case for where the next camera should go — "
                "derived from investigations that actually happened, not from a coverage "
                "model.",
                small,
            )
        )
        story.append(Spacer(1, 3))
        rows = [["Camera", "Location", "Times needed"]] + [
            [g["camera_ref"], g.get("name", "—"), str(g["times_implied"])] for g in journey_gaps
        ]
        story.append(_grid(rows, [28 * mm, 122 * mm, 20 * mm]))
    else:
        story.append(
            Paragraph(
                "None recorded in the window. This section fills as journey queries run: a "
                "gap appears when a route reconstruction repeatedly needed a position no "
                "camera covers. An empty section means the queries run so far were served "
                "by existing coverage — not that no gaps exist.",
                small,
            )
        )

    # ---- every camera finding ----
    if camera_gaps:
        story.append(Paragraph("Cameras with a gap", h2))
        rows = [["Camera", "Finding", "Detail"]] + [
            [g["camera_ref"], KIND_LABEL.get(g["kind"], g["kind"]), g.get("detail", "")]
            for g in camera_gaps
        ]
        story.append(_grid(rows, [28 * mm, 32 * mm, 110 * mm]))

    # ---- integrity ----
    story.append(Paragraph("Integrity", h2))
    story.append(
        Paragraph(
            "This document is signed with an Ed25519 detached signature over a canonical "
            "JSON manifest of every figure above. A recipient holding only this PDF and "
            "the public key can confirm the numbers were not altered after export; they "
            "need no access to SETU to do it.",
            small,
        )
    )
    story.append(Spacer(1, 3))
    story.append(_grid([["Manifest SHA-256", manifest_sha256]], [42 * mm, 128 * mm]))
    story.append(Spacer(1, 2))
    story.append(Paragraph(f"<b>Signature:</b> {signature_hex}", mono))
    story.append(Paragraph(f"<b>Public key:</b> {public_key}", mono))

    doc.build(story)
    return buffer.getvalue()


def export_gap_analysis(
    data: dict[str, Any], *, audit_seq: int | None, requested_by: str
) -> tuple[bytes, bytes, str, str]:
    """Return (pdf_bytes, manifest_bytes, signature_hex, public_key_hex)."""
    key = _load_signing_key()
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = build_manifest(data, audit_seq, generated)
    signature = key.sign(manifest)
    pdf = render_pdf(
        data,
        audit_seq=audit_seq,
        requested_by=requested_by,
        generated=generated,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        signature_hex=signature.hex(),
        public_key=public_key_hex(key),
    )
    return pdf, manifest, signature.hex(), public_key_hex(key)
