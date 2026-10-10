#!/usr/bin/env python
"""Compare the shipped appearance descriptor against ONNX embedding models.

Why
---
§3.2 of the build brief asks for a small MIT/Apache-licensed re-identification or general
embedding model to be evaluated against the handcrafted descriptor, with the comparison
recorded -- and says explicitly that a measured negative result is a legitimate outcome.
Finding 25 measured the handcrafted descriptor thoroughly and no model was ever put
beside it, so the brief's question had an answer for one candidate and no answer for the
alternative. This closes that.

Method, identical to finding 25 so the numbers are comparable
--------------------------------------------------------------
Ground truth comes from the tracker's own association, not from hand labelling: two crops
inside one `(camera_id, track_id)` are the same vehicle seen at different scales, poses
and lighting as it crosses the frame; two crops in different tracks are different
vehicles. Nothing is simulated and nothing is hand-labelled, so the same bias applies to
every candidate equally -- which is what makes the comparison fair even where the absolute
numbers are optimistic.

For each candidate the script reports the same four things finding 25 reports: the median
and p90 same-vehicle distance, the p10 and median different-vehicle distance, and a
recall / false-link-rate / precision table at a range of gates. The false-link rate is the
number that decides this: a search aid that proposes the wrong vehicle most of the time is
worse than one that proposes fewer.

Embeddings are the model's penultimate activations, L2-normalised, compared by cosine
distance -- the same metric the handcrafted descriptor is compared under, so no candidate
is flattered by a different measure.

Usage
-----
    python scripts/compare_appearance_models.py --model mobilenetv2=path/to/model.onnx
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

#: Gates the table is reported at. The shipped gate is 0.05; the rest bracket it so the
#: shape of the trade-off is visible rather than a single flattering row.
GATES = (0.02, 0.05, 0.10, 0.20, 0.39)

#: Cap on crops loaded. The pair count is quadratic, and a few hundred crops already give
#: tens of thousands of different-vehicle pairs.
MAX_CROPS = 400


def _pairs(labels: list[int]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Index pairs, split by whether the two crops are the same vehicle."""
    same: list[tuple[int, int]] = []
    diff: list[tuple[int, int]] = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            (same if labels[i] == labels[j] else diff).append((i, j))
    return same, diff


def _report(name: str, vectors: list[Any], labels: list[int]) -> None:
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    same, diff = _pairs(labels)
    if not same or not diff:
        print(f"\n{name}: not enough tracks to form both kinds of pair")
        return

    def distances(pairs: list[tuple[int, int]]) -> list[float]:
        a = matrix[[i for i, _ in pairs]]
        b = matrix[[j for _, j in pairs]]
        return [float(d) for d in 1.0 - np.sum(a * b, axis=1)]

    ds, dd = distances(same), distances(diff)
    print(f"\n{'=' * 66}\n{name}  ({matrix.shape[1]} dimensions)")
    print(f"  {len(same)} same-vehicle pairs, {len(diff)} different-vehicle pairs")
    print(f"  same vehicle      median {statistics.median(ds):.4f}   p90 {_pct(ds, 90):.4f}")
    print(f"  different vehicles   p10 {_pct(dd, 10):.4f}   median {statistics.median(dd):.4f}")
    print(f"\n  {'gate':>6} {'recall':>8} {'false-link':>11} {'precision':>10}")
    for gate in GATES:
        tp = sum(1 for d in ds if d <= gate)
        fp = sum(1 for d in dd if d <= gate)
        recall = tp / len(ds)
        # Of the different-vehicle pairs, how many would this gate have linked. The same
        # definition finding 25 uses, so the two tables sit side by side.
        false_link = fp / len(dd)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        print(f"  {gate:>6.2f} {recall:>8.3f} {false_link:>11.3f} {precision:>10.3f}")


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _embed_onnx(model_path: Path, crops: list[Any]) -> list[Any] | None:
    """Penultimate activations, globally pooled. None when the model will not load."""
    import numpy as np
    import onnxruntime as ort

    try:
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 -- a model that will not load is a result
        print(f"  {model_path.name}: will not load -- {type(exc).__name__}: {exc}")
        return None

    inp = session.get_inputs()[0]
    shape = [d if isinstance(d, int) else 1 for d in inp.shape]
    size = (shape[3], shape[2]) if len(shape) == 4 else (224, 224)

    import cv2

    # ImageNet preprocessing, which is what both candidate graphs were trained under.
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)

    out: list[Any] = []
    for crop in crops:
        rgb = cv2.cvtColor(cv2.resize(crop, size), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        batch = ((rgb - mean) / std).transpose(2, 0, 1)[None, ...].astype(np.float32)
        logits = session.run(None, {inp.name: batch})[0]
        out.append(np.asarray(logits).reshape(-1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="an ONNX model to compare; repeatable",
    )
    ap.add_argument("--max-crops", type=int, default=MAX_CROPS)
    args = ap.parse_args()

    import cv2
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from services.analytics import appearance
    from services.api.tenancy import set_admin_context
    from services.common.dburl import normalise_pg_url
    from services.common.paths import CROPS_DIR

    url = os.environ.get("SETU_MIGRATION_DATABASE_URL") or os.environ.get("SETU_DATABASE_URL")
    normalised = normalise_pg_url(url)
    if normalised:
        session = sessionmaker(bind=create_engine(normalised, future=True), future=True)()
    else:
        from services.api.db import get_sessionmaker

        session = get_sessionmaker()()
    set_admin_context(session)

    rows = list(
        session.execute(
            text(
                """
                SELECT s.crop_path, s.camera_id::text AS cam, s.track_id
                FROM vehicle_sighting s
                WHERE s.crop_path IS NOT NULL AND s.track_id IS NOT NULL
                ORDER BY s.camera_id, s.track_id, s.pts_ms
                LIMIT :cap
                """
            ),
            {"cap": args.max_crops},
        ).mappings()
    )
    session.close()

    crops: list[Any] = []
    labels: list[int] = []
    keys: dict[tuple[str, str], int] = {}
    for row in rows:
        path = CROPS_DIR / Path(row["crop_path"]).name
        image = cv2.imread(str(path)) if path.exists() else None
        if image is None:
            continue
        key = (row["cam"], str(row["track_id"]))
        labels.append(keys.setdefault(key, len(keys)))
        crops.append(image)

    tracks = len(set(labels))
    print(f"{len(crops)} crops over {tracks} tracks")
    if len(crops) < 10:
        print("too few crops to compare anything", file=sys.stderr)
        return 1

    # The shipped descriptor first, so every model is read against it rather than against
    # an abstract expectation.
    handcrafted: list[Any] = []
    kept: list[int] = []
    for crop, label in zip(crops, labels):
        described = appearance.describe(crop)
        if described is None:
            continue
        handcrafted.append(described.vector)
        kept.append(label)
    _report("shipped: 76-dimension CIELAB + edge descriptor", handcrafted, kept)

    for spec in args.model:
        name, _, path = spec.partition("=")
        vectors = _embed_onnx(Path(path), crops)
        if vectors is not None:
            _report(f"candidate: {name}", vectors, labels)

    print("\n" + "=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
