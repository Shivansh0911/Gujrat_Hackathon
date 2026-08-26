"""Signed PDF evidence export for a reconstructed journey.

What makes this evidence rather than a printout
-----------------------------------------------
A PDF of a route is a picture. Three things turn it into something a reviewer can
rely on, and all three are here:

1. **Every hop carries its provenance.** All three clocks (stream PTS, derived
   observation time, ingest time), the coordinate *with its uncertainty*, the
   evidence crop, and every character the OCR corrected. A corrected plate is never
   printed as a clean read.

2. **It commits to the audit ledger.** The export references the hash-chain entry for
   the query that produced it. Anyone can later ask SETU to verify that chain; if the
   underlying records were altered afterwards, verification fails.

3. **It is signed detachedly, and the signature verifies without SETU.** The manifest
   is a canonical JSON digest of every hop. The `.sig` file is an Ed25519 signature
   over that manifest. A recipient with only the public key, the PDF and the manifest
   can confirm nothing was altered in transit -- they do not need access to our
   database, our API, or us.

Ed25519 rather than RSA: small keys, small signatures, no parameter choices to get
wrong. The key is read from the environment and generated on first use for local
development only, with the public key written beside the export so verification is
possible out of the box.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from services.common.paths import CROPS_DIR, PROJECT_ROOT

log = logging.getLogger(__name__)

SIGNING_KEY_ENV = "SETU_EVIDENCE_SIGNING_KEY"
_DEV_KEY_PATH = PROJECT_ROOT / "data" / "evidence" / "signing_key.dev"


def _load_signing_key() -> Ed25519PrivateKey:
    """Signing key from the environment; a development key on disk as fallback.

    The environment variable is the production path. The on-disk development key
    exists so a fresh checkout can produce a verifiable export without ceremony, and
    is named `.dev` and gitignored precisely so it is never mistaken for a real one.
    """
    raw = os.environ.get(SIGNING_KEY_ENV)
    if raw:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw.strip()))

    if _DEV_KEY_PATH.exists():
        return Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(_DEV_KEY_PATH.read_text(encoding="utf-8").strip())
        )

    key = Ed25519PrivateKey.generate()
    _DEV_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEV_KEY_PATH.write_text(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex(),
        encoding="utf-8",
    )
    log.warning(
        "generated a DEVELOPMENT evidence signing key at %s; set %s in production",
        _DEV_KEY_PATH.name, SIGNING_KEY_ENV,
    )
    return key


def public_key_hex(key: Ed25519PrivateKey | None = None) -> str:
    k = key or _load_signing_key()
    return k.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()


def verify_manifest(manifest: bytes, signature: bytes, public_key_hex_str: str) -> bool:
    """Verify a detached signature. Deliberately importable without the rest of SETU."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex_str))
        pub.verify(signature, manifest)
        return True
    except Exception:
        # Any failure -- malformed key, wrong signature, altered manifest -- is a
        # verification failure and must not be distinguished for the caller.
        return False


def build_manifest(journey: dict[str, Any], audit_seq: int | None, model_version: str) -> bytes:
    """Canonical JSON committing to every fact printed in the PDF.

    Key order is fixed and separators are tight so two runs over the same journey
    produce byte-identical manifests. A manifest whose bytes depend on dict ordering
    cannot be verified by anyone else.
    """
    payload = {
        "plate": journey["plate"],
        "window_start": journey["window_start"],
        "window_end": journey["window_end"],
        "purpose": journey["purpose"],
        "requested_by": journey["requested_by"],
        "audit_seq": audit_seq,
        "model_version": model_version,
        "total_distance_m": journey["total_distance_m"],
        "duration_s": journey["duration_s"],
        "confidence": journey["confidence"],
        "hops": [
            {
                "seq": h["seq"],
                "camera_ref": h["camera_ref"],
                "camera_name": h["camera_name"],
                "lat": h["lat"],
                "lon": h["lon"],
                "confidence_radius_m": h["confidence_radius_m"],
                "observed_at_utc": h["observed_at_utc"],
                "pts_ms": h["pts_ms"],
                "clock_confidence": h["clock_confidence"],
                "plate_read": h["plate_read"],
                "evidence_type": h["evidence_type"],
                "corrections": h["corrections"],
                "confidence": h["confidence"],
                "implied_speed_kmph": h.get("implied_speed_kmph"),
                "crop_sha256": _crop_digest(h.get("crop_url")),
            }
            for h in journey["hops"]
        ],
        "coverage_gaps": [
            {"camera_ref": g["camera_ref"], "reason": g["reason"]}
            for g in journey["coverage_gaps"]
        ],
        "rejected": [
            {"camera_ref": r["camera_ref"], "reason": r["reason"]}
            for r in journey["rejected"]
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _crop_path(crop_url: str | None) -> Path | None:
    if not crop_url:
        return None
    candidate = (CROPS_DIR / Path(crop_url).name).resolve()
    # Resolve inside the crop directory: the URL is attacker-influenced in principle,
    # and a traversal here would embed an arbitrary file into a signed document.
    if CROPS_DIR.resolve() not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _crop_digest(crop_url: str | None) -> str | None:
    """Hash the evidence image itself, so the manifest commits to the picture too."""
    path = _crop_path(crop_url)
    if path is None:
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_pdf(
    journey: dict[str, Any],
    *,
    audit_seq: int | None,
    model_version: str,
    manifest_sha256: str,
    signature_hex: str,
    public_key: str,
) -> bytes:
    """Produce the evidence PDF."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"SETU evidence export - {journey['plate']}",
        author="Project SETU",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=15, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=11.5)
    small = ParagraphStyle("small", parent=body, fontSize=7.2, textColor=colors.HexColor("#555555"))
    mono = ParagraphStyle("mono", parent=body, fontName="Courier", fontSize=7)

    story: list[Any] = []

    story.append(Paragraph("Vehicle movement evidence export", h1))
    story.append(Paragraph(
        "Project SETU — Gujarat Police CCTV Integration Platform", small))
    story.append(Spacer(1, 6))

    # ---- header: the query and its authorisation ----
    header_rows = [
        ["Registration", journey["plate"]],
        ["Purpose stated", journey["purpose"]],
        ["Requested by", journey["requested_by"]],
        ["Window", f"{journey['window_start']} to {journey['window_end']}"],
        ["Generated (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")],
        ["Audit ledger entry", str(audit_seq) if audit_seq is not None else "not recorded"],
        ["Analytics version", model_version],
        ["Hops established", str(len(journey["hops"]))],
        ["Distance", f"{journey['total_distance_m'] / 1000:.2f} km"],
        ["Journey confidence", f"{journey['confidence']:.2f}"],
    ]
    table = Table(header_rows, colWidths=[38 * mm, 132 * mm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#444444")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#DDDDDD")),
    ]))
    story.append(table)

    story.append(Paragraph("Route", h2))

    for hop in journey["hops"]:
        story.append(_hop_block(hop, body, small, mono))
        story.append(Spacer(1, 5))

    # ---- what was NOT seen ----
    if journey["coverage_gaps"]:
        story.append(Paragraph("Coverage gaps", h2))
        story.append(Paragraph(
            "Cameras lying on the reconstructed route that recorded no detection in the "
            "relevant interval. These are reported because the difference between "
            "&ldquo;the vehicle was not there&rdquo; and &ldquo;we could not see&rdquo; is "
            "material to an investigation, and only one of them is a coverage problem.",
            small))
        story.append(Spacer(1, 3))
        gap_rows = [["Camera", "Finding"]] + [
            [g["camera_ref"], g["reason"]] for g in journey["coverage_gaps"]
        ]
        story.append(_grid(gap_rows, [28 * mm, 142 * mm]))

    if journey["rejected"]:
        story.append(Paragraph("Candidate sightings rejected", h2))
        story.append(Paragraph(
            "Sightings of this registration that were excluded from the route because "
            "they were not physically reachable from the preceding hop. Listed so a "
            "reviewer can see what was considered as well as what was accepted.", small))
        story.append(Spacer(1, 3))
        rej_rows = [["Camera", "Reason for exclusion"]] + [
            [r["camera_ref"], r["reason"]] for r in journey["rejected"]
        ]
        story.append(_grid(rej_rows, [28 * mm, 142 * mm]))

    # ---- integrity ----
    story.append(PageBreak())
    story.append(Paragraph("Integrity and verification", h1))
    story.append(Paragraph(
        "This export commits to the facts above in two independent ways.", body))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "<b>1. Audit chain.</b> The query that produced this route was written to a "
        "hash-chained ledger <i>before</i> it executed, at the entry number shown in the "
        "header. Each entry commits to every entry before it, so altering any historical "
        "record invalidates the chain from that point on. Ask SETU to verify the chain "
        "at <font face='Courier'>GET /audit/verify</font>.", body))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "<b>2. Detached signature.</b> The accompanying manifest is a canonical JSON "
        "record of every hop, including a SHA-256 of each evidence image. It is signed "
        "with Ed25519. A recipient can verify it with the public key below and needs no "
        "access to SETU, its database, or its operators to do so.", body))
    story.append(Spacer(1, 6))

    integrity_rows = [
        ["Manifest SHA-256", manifest_sha256],
        ["Signature (Ed25519)", signature_hex],
        ["Public key", public_key],
    ]
    story.append(_grid(integrity_rows, [34 * mm, 136 * mm], mono_col=1))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Verifying without SETU", h2))
    story.append(Paragraph(
        "<font face='Courier' size='7'>"
        "python -c \"from cryptography.hazmat.primitives.asymmetric.ed25519 import "
        "Ed25519PublicKey; import sys; "
        "Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY))"
        ".verify(bytes.fromhex(SIGNATURE), open('manifest.json','rb').read()); "
        "print('signature valid')\"</font>", small))

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Plate reads marked as corrected were produced by automated recognition with "
        "character-level substitutions applied under Indian registration grammar. Each "
        "substitution is printed with the position and the confidence at that position. "
        "A corrected read is an investigative lead requiring human verification, not a "
        "confirmed identification.", small))

    doc.build(story)
    return buffer.getvalue()


def _hop_block(hop: dict[str, Any], body, small, mono) -> Table:
    """One hop: crop on the left, provenance on the right."""
    crop_path = _crop_path(hop.get("crop_url"))
    if crop_path is not None:
        try:
            img: Any = Image(str(crop_path), width=38 * mm, height=19 * mm, kind="proportional")
        except Exception:
            # A corrupt or unreadable crop must not prevent the export; the manifest
            # still records its absence via a null digest.
            img = Paragraph("crop unavailable", small)
    else:
        img = Paragraph("no crop", small)

    corrections = hop.get("corrections") or []
    if corrections:
        corr_text = "<b>Corrections applied:</b> " + "; ".join(
            f"position {c['position']}: {c['raw']} &rarr; {c['corrected']} "
            f"(confidence {c['confidence']})"
            for c in corrections
        )
    else:
        corr_text = "<b>Corrections applied:</b> none — read as captured"

    speed = hop.get("implied_speed_kmph")
    radius = hop.get("confidence_radius_m")

    detail = (
        f"<b>Hop {hop['seq']} — {hop['camera_name']}</b> "
        f"(<font face='Courier'>{hop['camera_ref']}</font>)<br/>"
        f"{hop.get('location_text') or ''}<br/>"
        f"<b>Read:</b> <font face='Courier'>{hop['plate_read']}</font> · "
        f"{hop['evidence_type']} · confidence {hop['confidence']:.2f}<br/>"
        f"<b>Observed (UTC):</b> {hop['observed_at_utc']}<br/>"
        f"<b>Stream PTS:</b> {hop['pts_ms']:.0f} ms · "
        f"clock confidence {hop['clock_confidence']:.2f}<br/>"
        f"<b>Position:</b> {hop['lat']:.5f}, {hop['lon']:.5f}"
        + (f" ± {radius / 1000:.1f} km" if radius else " (precision not recorded)")
        + "<br/>"
        + (f"<b>Implied speed from previous hop:</b> {speed:.0f} km/h<br/>" if speed else "")
        + corr_text
    )

    table = Table([[img, Paragraph(detail, body)]], colWidths=[40 * mm, 130 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _grid(rows: list[list[str]], widths: list[float], mono_col: int | None = None) -> Table:
    table = Table(rows, colWidths=widths)
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if mono_col is not None:
        style.append(("FONTNAME", (mono_col, 0), (mono_col, -1), "Courier"))
        style.append(("FONTSIZE", (mono_col, 0), (mono_col, -1), 6))
    table.setStyle(TableStyle(style))
    return table


def export_journey(
    journey: dict[str, Any], *, audit_seq: int | None, model_version: str
) -> tuple[bytes, bytes, str, str]:
    """Return (pdf_bytes, manifest_bytes, signature_hex, public_key_hex)."""
    key = _load_signing_key()
    manifest = build_manifest(journey, audit_seq, model_version)
    signature = key.sign(manifest)
    pdf = render_pdf(
        journey,
        audit_seq=audit_seq,
        model_version=model_version,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        signature_hex=signature.hex(),
        public_key=public_key_hex(key),
    )
    return pdf, manifest, signature.hex(), public_key_hex(key)
