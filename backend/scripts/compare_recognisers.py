#!/usr/bin/env python
"""Score every candidate OCR model against the annotated crops.

The first accuracy measurement returned precision 0.0 with a character error rate of
39.8% -- the recogniser was getting most characters right and the whole plate wrong,
every time. That pattern points at model fit rather than at the pipeline around it,
and the question "would a different recogniser do better?" is now answerable rather
than arguable, because `data/seed/anpr_ground_truth.csv` says what each crop actually
contains.

This runs each candidate model directly over the committed evidence crops and scores
it against that annotation. It deliberately isolates the recogniser: the same crops,
the same grammar, no re-ingest, no detector variance. Only legible crops count toward
accuracy; crops annotated `unreadable` are scored separately, because a model that
stays silent on an illegible crop is behaving correctly and one that emits a
confident registration is not.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.analytics.plate_grammar import normalise_plate  # noqa: E402
from services.common.cv_env import cv2  # noqa: E402
from services.common.paths import CROPS_DIR, DATA_DIR, EVIDENCE_DIR  # noqa: E402

ANNOTATION_CSV = DATA_DIR / "seed" / "anpr_ground_truth.csv"
UNREADABLE = {"unreadable", "illegible", "none", "n/a", "-"}

CANDIDATES = [
    "cct-s-v1-global-model",
    "cct-s-v2-global-model",
    "cct-xs-v2-global-model",
    "global-plates-mobile-vit-v2-model",
]


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def load_annotations() -> list[dict[str, str]]:
    with ANNOTATION_CSV.open(encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["true_plate"].strip()]
    # One row per distinct image. The REPLAY cameras are four copies of the same
    # frame; scoring all four would weight those crops 4x for no extra information.
    seen: dict[str, dict[str, str]] = {}
    for r in rows:
        stem = r["crop_file"].rsplit(".", 1)[0]
        key = "_".join(stem.split("_")[1:]) if stem.startswith("REPLAY-") else stem
        seen.setdefault(key, r)
    return list(seen.values())


def score_model(name: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    from fast_plate_ocr import LicensePlateRecognizer

    started = time.monotonic()
    impl = LicensePlateRecognizer(name)  # type: ignore[arg-type]

    correct = 0
    wrong: list[tuple[str, str, str]] = []
    distance = 0
    chars = 0
    legible = 0
    asserted_on_illegible = 0
    illegible = 0

    for row in rows:
        path = CROPS_DIR / row["crop_file"]
        if not path.exists():
            continue
        crop = cv2.imread(str(path))
        if crop is None:
            continue

        preds = impl.run(crop, return_confidence=True)
        text = ""
        if preds:
            pred = preds[0]
            raw = str(getattr(pred, "plate", "") or "")
            text = "".join(c for c in raw if c not in "_ ")
        read = normalise_plate(text).normalised if text else ""

        truth_raw = row["true_plate"].strip()
        if truth_raw.lower() in UNREADABLE:
            illegible += 1
            if read:
                asserted_on_illegible += 1
            continue

        legible += 1
        truth = truth_raw.upper().replace(" ", "").replace("-", "")
        if read == truth:
            correct += 1
        else:
            wrong.append((row["crop_file"], read, truth))
        distance += levenshtein(read, truth)
        chars += len(truth)

    asserted = legible + asserted_on_illegible
    return {
        "model": name,
        "crops_legible": legible,
        "crops_illegible": illegible,
        "correct": correct,
        "wrong": len(wrong),
        "asserted_on_illegible": asserted_on_illegible,
        "precision": round(correct / asserted, 4) if asserted else 0.0,
        "recall": round(correct / legible, 4) if legible else 0.0,
        "character_error_rate": round(distance / chars, 4) if chars else 0.0,
        "seconds": round(time.monotonic() - started, 1),
        "examples": [{"crop": c, "read": r, "truth": t} for c, r, t in wrong[:6]],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default=",".join(CANDIDATES))
    args = ap.parse_args()

    if not ANNOTATION_CSV.exists():
        print("no annotation file; run ground_truth.py annotate and fill it in first")
        return 2

    rows = load_annotations()
    print(f"{len(rows)} distinct annotated crops")

    results = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\nscoring {name} ...", flush=True)
        try:
            res = score_model(name, rows)
        except Exception as exc:
            print(f"  failed: {type(exc).__name__}: {exc}")
            continue
        results.append(res)
        print(
            f"  precision {res['precision']:.1%}  recall {res['recall']:.1%}  "
            f"CER {res['character_error_rate']:.1%}  "
            f"({res['correct']}/{res['crops_legible']} exact, "
            f"{res['asserted_on_illegible']} asserted on illegible, {res['seconds']}s)"
        )

    if not results:
        print("no model scored successfully")
        return 1

    results.sort(key=lambda r: (-r["correct"], r["character_error_rate"]))
    best = results[0]

    print("\n" + "=" * 70)
    print("RECOGNISER COMPARISON")
    print("=" * 70)
    print(f"  {'model':<38} {'exact':>7} {'prec':>7} {'CER':>7}")
    for r in results:
        print(
            f"  {r['model']:<38} {r['correct']:>3}/{r['crops_legible']:<3} "
            f"{r['precision']:>6.1%} {r['character_error_rate']:>6.1%}"
        )
    print(f"\n  best by exact matches: {best['model']}")
    print("=" * 70)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = EVIDENCE_DIR / f"recogniser-comparison-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_utc": datetime.now(timezone.utc).isoformat(),
                "annotation_file": str(ANNOTATION_CSV.name),
                "distinct_crops": len(rows),
                "results": results,
                "best_by_exact_matches": best["model"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
