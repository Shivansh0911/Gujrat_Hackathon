"""The gap-analysis export must be a document, and the signature must mean something.

Two things are worth testing here and one thing is not. Not worth testing: that
reportlab draws tables. Worth testing: that the bytes are a PDF at all, and that the
detached signature actually commits to the figures printed -- because a signature that
verifies after the numbers change is worse than no signature, since it lends authority
to an altered document.
"""

from __future__ import annotations

import copy

from services.api.evidence_export import verify_manifest
from services.api.gap_export import build_manifest, export_gap_analysis

ANALYSIS = {
    "summary": {"cameras_total": 34, "districts_covered": 10, "no_coordinate": 2},
    "districts": [
        {
            "district": "Ahmedabad",
            "cameras_total": 9,
            "cameras_placed": 8,
            "coverage_confidence": 0.62,
            "findings": ["1 camera(s) with no coordinate"],
        }
    ],
    "camera_gaps": [
        {
            "camera_ref": "GJ-CAM-0007",
            "kind": "no_coordinate",
            "detail": "No coordinate on record.",
            "name": "Paldi junction",
        }
    ],
    "journey_gaps": [
        {"camera_ref": "GJ-CAM-0019", "name": "Visat circle", "times_implied": 3}
    ],
}


def test_export_produces_a_verifiable_pdf() -> None:
    pdf, manifest, signature, public_key = export_gap_analysis(
        ANALYSIS, audit_seq=41, requested_by="admin"
    )

    assert pdf.startswith(b"%PDF-"), "the export must be a PDF, not an error page"
    assert len(pdf) > 2000, "a report with ten districts and a gap table cannot be tiny"
    assert verify_manifest(manifest, bytes.fromhex(signature), public_key)


def test_manifest_is_canonical_and_commits_to_the_figures() -> None:
    """Byte-identical for the same analysis; different the moment a number moves.

    The first half is what makes the signature verifiable by somebody else -- a
    manifest whose bytes depend on dict ordering cannot be re-derived. The second is
    what makes it worth signing.
    """
    a = build_manifest(ANALYSIS, 41, "2026-08-31T00:00:00+00:00")
    b = build_manifest(copy.deepcopy(ANALYSIS), 41, "2026-08-31T00:00:00+00:00")
    assert a == b

    altered = copy.deepcopy(ANALYSIS)
    altered["journey_gaps"][0]["times_implied"] = 9  # type: ignore[index]
    assert build_manifest(altered, 41, "2026-08-31T00:00:00+00:00") != a


def test_signature_fails_against_an_altered_manifest() -> None:
    _, manifest, signature, public_key = export_gap_analysis(
        ANALYSIS, audit_seq=41, requested_by="admin"
    )
    tampered = manifest.replace(b'"cameras_total":34', b'"cameras_total":99')
    assert tampered != manifest, "the fixture must contain the value being tampered with"
    assert not verify_manifest(tampered, bytes.fromhex(signature), public_key)


def test_empty_analysis_still_renders() -> None:
    """A clean estate is a legitimate result, not an error.

    The report has to say "nothing found" as a document rather than crashing on an
    empty table -- reportlab raises on a Table with no rows, and the version of this
    that only ever ran against the seeded database would not have noticed.
    """
    empty = {"summary": {}, "districts": [], "camera_gaps": [], "journey_gaps": []}
    pdf, _, _, _ = export_gap_analysis(empty, audit_seq=None, requested_by="admin")
    assert pdf.startswith(b"%PDF-")
