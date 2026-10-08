"""Bounding-box geometry shared by the plate and vehicle trackers.

Extracted from `anpr.py` when vehicle tracking arrived and needed exactly the same
three primitives. Copying them would have been quicker and would have let the two
trackers drift: the motion-tolerant association gate in `PlateTracker` is tuned against
measured footage (see its docstring), and a second implementation of `diagonal` that
rounded differently would silently retune it.

Boxes are `(x1, y1, x2, y2)` in frame pixels throughout this codebase.
"""

from __future__ import annotations

Box = tuple[int, int, int, int]


def iou(a: Box, b: Box) -> float:
    """Intersection over union. 0.0 when the boxes do not overlap at all."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def centre(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def diagonal(box: Box) -> float:
    """Box diagonal, floored at 1.0 so it is always safe to divide by."""
    w, h = box[2] - box[0], box[3] - box[1]
    return max(1.0, float((w * w + h * h) ** 0.5))


__all__ = ["Box", "centre", "diagonal", "iou"]
