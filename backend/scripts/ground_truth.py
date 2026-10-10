#!/usr/bin/env python
"""Measure ANPR accuracy against human annotation.

Why a human is in the loop
--------------------------
Every accuracy figure quoted so far has been a *pipeline rate* -- how many frames
passed the gate, how many boxes the detector found, how many strings parsed as legal
registrations. None of those is recall or precision, because none of them knows what
the plate actually was. A system cannot grade its own reading.

So this runs in two passes:

    python scripts/ground_truth.py annotate     # writes a CSV of every crop
    <a human fills in the `true_plate` column>
    python scripts/ground_truth.py score        # computes the real numbers

The annotation file is committed. It is the only artefact in this repository that
records what was genuinely in the footage, and it is what makes the accuracy claim
checkable by someone who does not trust us.

What is measured
----------------
* **Precision** — of the plates we asserted, what fraction were right. A system that
  reports a wrong registration to an investigator is worse than one that reports
  nothing, so this is the number that matters most.
* **Recall** — of the plates a human could read, what fraction we also read
  correctly. Crops annotated `unreadable` are excluded from the denominator: failing
  to read something no human can read is not a miss, and counting it as one would
  flatter nothing and confuse everything.
* **Character error rate** — normalised edit distance across all comparable reads.
  A system that is 90% right at the character level is a different proposition from
  one that is 40% right, even at identical plate-level accuracy.
* **Correction efficacy** — did grammar correction help or hurt? Measured by
  comparing accuracy on reads that were corrected against those that were not.

Two denominators, and the difference is not cosmetic
----------------------------------------------------
The four `REPLAY-*` cameras replay one clip, so a crop cut from it exists four times
over, under four camera references, byte for byte identical. `annotate` enumerates crops
per camera and cannot see that: of 27 annotated rows, 12 are distinct images and five of
those appear four times each.

Scored per row, the headline is therefore weighted by how many cameras happened to be
pointed at the same recording -- a property of the demonstration rig, not of the
recogniser. So both figures are reported: **per annotated crop**, which is the number
every document has quoted and stays comparable with them, and **per distinct image**,
which is the recogniser's accuracy. Neither is a correction of the other and both are
printed, because collapsing to one invites exactly the question of which was chosen.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from services.api.db import get_sessionmaker  # noqa: E402
from services.api.tenancy import set_admin_context  # noqa: E402
from services.common import evidence, redact  # noqa: E402
from services.common.paths import CROPS_DIR, DATA_DIR  # noqa: E402

log = logging.getLogger("ground-truth")

ANNOTATION_CSV = DATA_DIR / "seed" / "anpr_ground_truth.csv"

FIELDS = [
    "crop_file",
    "camera_ref",
    "observed_at_utc",
    "pipeline_read",
    "pipeline_confidence",
    "corrections_applied",
    "true_plate",
    "notes",
]

# Annotation values that mean "no plate a human could read here". Anything else in
# the `true_plate` column is treated as the correct registration.
UNREADABLE = {"unreadable", "illegible", "none", "n/a", "-"}


def _levenshtein(a: str, b: str) -> int:
    """Standard edit distance. Small strings, so the simple DP is fine."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def annotate(args: argparse.Namespace) -> int:
    """Emit a CSV of every detection with its crop, for a human to fill in."""
    session = get_sessionmaker()()
    set_admin_context(session)
    try:
        rows = (
            session.execute(
                text(
                    """
                SELECT d.crop_path, d.plate_normalised, d.confidence,
                       d.observed_at_utc, d.corrections, c.camera_ref
                FROM detection d
                JOIN camera c ON c.id = d.camera_id
                WHERE d.crop_path IS NOT NULL
                ORDER BY c.camera_ref, d.observed_at_utc
                """
                )
            )
            .mappings()
            .all()
        )
    finally:
        session.close()

    if not rows:
        log.error("no detections with crops; run the ANPR pipeline first")
        return 2

    # Preserve annotations already made, so re-running after a new ingest does not
    # discard work. The crop filename is the key: it encodes camera, PTS and read.
    existing: dict[str, dict[str, str]] = {}
    if ANNOTATION_CSV.exists():
        with ANNOTATION_CSV.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("true_plate", "").strip():
                    existing[row["crop_file"]] = row
        log.info("preserving %d existing annotation(s)", len(existing))

    # Crops are deduplicated by filename: the demo replays one clip through several
    # cameras, so the same image legitimately appears against several detections.
    # Annotating it once is enough, and annotating it four times invites disagreement.
    seen: set[str] = set()
    out_rows: list[dict[str, str]] = []
    for row in rows:
        crop_file = Path(row["crop_path"]).name
        if crop_file in seen:
            continue
        seen.add(crop_file)
        prior = existing.get(crop_file, {})
        out_rows.append(
            {
                "crop_file": crop_file,
                "camera_ref": row["camera_ref"],
                "observed_at_utc": row["observed_at_utc"].isoformat(),
                "pipeline_read": row["plate_normalised"],
                "pipeline_confidence": f"{float(row['confidence'] or 0):.4f}",
                "corrections_applied": str(len(row["corrections"] or [])),
                "true_plate": prior.get("true_plate", ""),
                "notes": prior.get("notes", ""),
            }
        )

    ANNOTATION_CSV.parent.mkdir(parents=True, exist_ok=True)
    with ANNOTATION_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(out_rows)

    todo = sum(1 for r in out_rows if not r["true_plate"])
    print(f"\nWrote {ANNOTATION_CSV}")
    print(f"  {len(out_rows)} unique crop(s), {todo} awaiting annotation")
    print(f"  Crops are in {CROPS_DIR}")
    print()
    print("  Open each crop, read the plate, and fill the `true_plate` column.")
    print("  Where no plate is legible to a human, write: unreadable")
    print("  Then run: python scripts/ground_truth.py score\n")
    return 0


def _image_digest(crop_file: str) -> str:
    """Content hash of a crop, or its filename when the file is gone.

    Identity by content rather than by name, because the duplication this exists to
    find is four camera references over one recording: the files have four names and
    one set of bytes.
    """
    path = CROPS_DIR / crop_file
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        # A missing crop cannot be shown to duplicate anything, so it counts once.
        return f"missing:{crop_file}"


def _per_distinct_image(
    readable: list[dict[str, str]], unreadable: list[dict[str, str]]
) -> dict[str, object]:
    """Re-score with each distinct crop image counted once.

    Keeps the first row for each image. Which row is kept does not matter: the duplicates
    are the same bytes through the same recogniser, so they carry the same read.
    """
    seen: set[str] = set()
    uniq_readable: list[dict[str, str]] = []
    for row in readable:
        digest = _image_digest(row["crop_file"])
        if digest not in seen:
            seen.add(digest)
            uniq_readable.append(row)
    uniq_unreadable: list[dict[str, str]] = []
    for row in unreadable:
        digest = _image_digest(row["crop_file"])
        if digest not in seen:
            seen.add(digest)
            uniq_unreadable.append(row)

    correct = 0
    distance = chars = 0
    for row in uniq_readable:
        truth = row["true_plate"].strip().upper().replace(" ", "").replace("-", "")
        read = row["pipeline_read"].strip().upper()
        if read == truth:
            correct += 1
        distance += _levenshtein(read, truth)
        chars += len(truth)

    asserted = len(uniq_readable) + sum(1 for r in uniq_unreadable if r["pipeline_read"].strip())
    precision = correct / asserted if asserted else 0.0
    recall = correct / len(uniq_readable) if uniq_readable else 0.0
    return {
        "distinct_images": len(seen),
        "duplicate_rows_collapsed": (len(readable) + len(unreadable)) - len(seen),
        "legible_to_a_human": len(uniq_readable),
        "reads_correct": correct,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "character_error_rate": round(distance / chars, 4) if chars else 0.0,
    }


def score(args: argparse.Namespace) -> int:
    """Compute precision, recall and character error rate against the annotations."""
    if not ANNOTATION_CSV.exists():
        log.error("no annotation file; run `ground_truth.py annotate` first")
        return 2

    with ANNOTATION_CSV.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    annotated = [r for r in rows if r["true_plate"].strip()]
    if not annotated:
        log.error("no rows annotated yet; fill the true_plate column in %s", ANNOTATION_CSV)
        return 2

    readable = [r for r in annotated if r["true_plate"].strip().lower() not in UNREADABLE]
    unreadable = [r for r in annotated if r["true_plate"].strip().lower() in UNREADABLE]

    correct: list[dict[str, str]] = []
    wrong: list[dict[str, str]] = []
    total_distance = 0
    total_chars = 0

    for row in readable:
        truth = row["true_plate"].strip().upper().replace(" ", "").replace("-", "")
        read = row["pipeline_read"].strip().upper()
        if read == truth:
            correct.append(row)
        else:
            wrong.append(row)
        total_distance += _levenshtein(read, truth)
        total_chars += len(truth)

    # A read asserted on a crop a human found unreadable is a false positive: the
    # system produced a registration where no evidence of one exists.
    false_on_unreadable = [r for r in unreadable if r["pipeline_read"].strip()]

    distinct = _per_distinct_image(readable, unreadable)

    asserted = len(readable) + len(false_on_unreadable)
    precision = len(correct) / asserted if asserted else 0.0
    recall = len(correct) / len(readable) if readable else 0.0
    cer = total_distance / total_chars if total_chars else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # Did grammar correction help or hurt? Comparing accuracy on corrected versus
    # uncorrected reads is the only way to know, and the answer decides whether the
    # correction cap is set right.
    corrected = [r for r in readable if int(r["corrections_applied"] or 0) > 0]
    uncorrected = [r for r in readable if int(r["corrections_applied"] or 0) == 0]
    corrected_ok = sum(1 for r in corrected if r in correct)
    uncorrected_ok = sum(1 for r in uncorrected if r in correct)

    payload = {
        "annotation_file": str(ANNOTATION_CSV.relative_to(DATA_DIR.parent)),
        "crops_total": len(rows),
        "crops_annotated": len(annotated),
        "crops_readable_by_human": len(readable),
        "crops_unreadable_by_human": len(unreadable),
        "reads_correct": len(correct),
        "reads_wrong": len(wrong),
        "reads_asserted_on_unreadable": len(false_on_unreadable),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "character_error_rate": round(cer, 4),
        "per_distinct_image": distinct,
        "correction_efficacy": {
            "corrected_reads": len(corrected),
            "corrected_correct": corrected_ok,
            "corrected_accuracy": round(corrected_ok / len(corrected), 4) if corrected else None,
            "uncorrected_reads": len(uncorrected),
            "uncorrected_correct": uncorrected_ok,
            "uncorrected_accuracy": (
                round(uncorrected_ok / len(uncorrected), 4) if uncorrected else None
            ),
        },
        "errors": [
            {
                "crop_file": r["crop_file"],
                "pipeline_read": r["pipeline_read"],
                "true_plate": r["true_plate"],
                "edit_distance": _levenshtein(
                    r["pipeline_read"].strip().upper(),
                    r["true_plate"].strip().upper().replace(" ", "").replace("-", ""),
                ),
                "corrections_applied": int(r["corrections_applied"] or 0),
                "confidence": float(r["pipeline_confidence"] or 0),
            }
            for r in wrong
        ],
        "method": (
            "Precision counts every asserted registration, including those asserted on "
            "crops a human found unreadable. Recall is over crops a human could read: "
            "failing to read what no human can read is not a miss. Character error rate "
            "is total Levenshtein distance over total ground-truth characters. "
            "`per_distinct_image` repeats all three with each crop image counted once, "
            "because the replay cameras share one recording and a crop cut from it is "
            "annotated once per camera."
        ),
    }

    print("\n" + "=" * 62)
    print("ANPR ACCURACY AGAINST HUMAN ANNOTATION")
    print("=" * 62)
    print(f"  crops annotated          : {len(annotated)} of {len(rows)}")
    print(f"  legible to a human       : {len(readable)}")
    print(f"  illegible to a human     : {len(unreadable)}")
    print()
    print(f"  reads correct            : {len(correct)}")
    print(f"  reads wrong              : {len(wrong)}")
    print(f"  asserted on illegible    : {len(false_on_unreadable)}")
    print()
    print(f"  PRECISION                : {precision:.1%}")
    print(f"  RECALL                   : {recall:.1%}")
    print(f"  F1                       : {f1:.3f}")
    print(f"  CHARACTER ERROR RATE     : {cer:.1%}")
    if distinct["duplicate_rows_collapsed"]:
        print()
        print(
            f"  the same {distinct['duplicate_rows_collapsed']} row(s) are duplicate images "
            f"-- the replay cameras share one recording"
        )
        print(f"  per distinct image       : {distinct['distinct_images']} crop(s)")
        print(f"    PRECISION              : {float(distinct['precision']):.1%}")
        print(f"    RECALL                 : {float(distinct['recall']):.1%}")
        print(f"    CHARACTER ERROR RATE   : {float(distinct['character_error_rate']):.1%}")
    if corrected:
        acc = corrected_ok / len(corrected)
        print(f"\n  corrected reads accuracy : {acc:.1%} over {len(corrected)} read(s)")
    if uncorrected:
        acc = uncorrected_ok / len(uncorrected)
        print(f"  uncorrected accuracy     : {acc:.1%} over {len(uncorrected)} read(s)")
    if wrong:
        print("\n  worst errors:")
        for e in sorted(payload["errors"], key=lambda x: -x["edit_distance"])[:6]:
            print(
                f"    read {e['pipeline_read']:<12} truth {e['true_plate']:<12} "
                f"distance {e['edit_distance']}  conf {e['confidence']:.2f}"
            )
    print("=" * 62 + "\n")

    if args.emit_evidence:
        md = _markdown(payload)
        j, m = evidence.write("anpr-accuracy", payload, md)
        print(f"evidence: {j.name}, {m.name}\n")
    return 0


def _markdown(p: dict) -> str:
    ce = p["correction_efficacy"]
    lines = [
        "# ANPR accuracy against human annotation",
        "",
        "Measured, not estimated. A human read every evidence crop and recorded the",
        "true registration; these figures compare the pipeline against that record.",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| Precision | **{p['precision']:.1%}** |",
        f"| Recall | **{p['recall']:.1%}** |",
        f"| F1 | {p['f1']:.3f} |",
        f"| Character error rate | {p['character_error_rate']:.1%} |",
        "",
        "| population | count |",
        "|---|---:|",
        f"| Crops annotated | {p['crops_annotated']} of {p['crops_total']} |",
        f"| Legible to a human | {p['crops_readable_by_human']} |",
        f"| Illegible to a human | {p['crops_unreadable_by_human']} |",
        f"| Reads correct | {p['reads_correct']} |",
        f"| Reads wrong | {p['reads_wrong']} |",
        f"| Asserted on an illegible crop | {p['reads_asserted_on_unreadable']} |",
        "",
        "## Did grammar correction help?",
        "",
        f"- Corrected reads: {ce['corrected_reads']}, "
        f"accuracy {ce['corrected_accuracy'] if ce['corrected_accuracy'] is not None else 'n/a'}",
        f"- Uncorrected reads: {ce['uncorrected_reads']}, "
        f"accuracy {ce['uncorrected_accuracy'] if ce['uncorrected_accuracy'] is not None else 'n/a'}",
        "",
        "## Method",
        "",
        p["method"],
        "",
    ]
    if p["errors"]:
        lines += [
            "## Every error",
            "",
            "| crop | read | truth | distance | corrections | confidence |",
            "|---|---|---|---:|---:|---:|",
        ]
        for e in sorted(p["errors"], key=lambda x: -x["edit_distance"]):
            lines.append(
                f"| `{e['crop_file']}` | `{e['pipeline_read']}` | `{e['true_plate']}` | "
                f"{e['edit_distance']} | {e['corrections_applied']} | {e['confidence']:.2f} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("annotate", help="write the annotation CSV for a human to fill in")
    a.set_defaults(func=annotate)

    s = sub.add_parser("score", help="compute accuracy against the annotations")
    s.add_argument("--emit-evidence", action="store_true")
    s.set_defaults(func=score)

    args = ap.parse_args()
    redact.install(level=logging.INFO)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
