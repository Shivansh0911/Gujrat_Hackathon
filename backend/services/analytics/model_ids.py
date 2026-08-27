"""Which analytics models are in use. One source of truth, no heavy imports.

This module deliberately imports nothing. The evidence exporter needs to name the
recogniser in a signed manifest, and importing `anpr` to ask it would drag OpenCV and
ONNX Runtime into the API process at startup for the sake of a string.

It exists because the name was previously written out by hand in the exporter. When
the recogniser changed, the signed manifest went on naming the old one. A signed
document that asserts the wrong provenance is worse than one that asserts none,
because a recipient has every reason to believe it.
"""

from __future__ import annotations

#: YOLOv9-t, 384px, end-to-end ONNX. MIT (see ADR 0003).
DETECTOR_MODEL = "yolo-v9-t-384-license-plate-end2end"

#: CCT ONNX recogniser. MIT.
#:
#: **Ten slots is a hard requirement, not a preference.** A model's `max_plate_slots`
#: is its number of classification heads, so it cannot emit a plate longer than that
#: under any circumstances. Indian registrations run to ten characters (XX00XX0000).
#: The 9-slot `cct-s-v1-global-model` was the original default, which meant every
#: full-length Indian plate it ever read was wrong before inference began -- measured
#: at 0% plate-level accuracy, with every single read exactly nine characters long.
#: Check `max_plate_slots` in the model's config before changing this.
RECOGNISER_MODEL = "cct-s-v2-global-model"

#: Bumped whenever the detector, recogniser or fusion changes in a way that could
#: alter a read. Printed on every evidence export so a disputed result can be tied to
#: the exact analytics that produced it.
#:
#: 2.0.0 -- recogniser moved to a 10-slot model, track association gained a
#: motion-tolerant gate (IoU alone associated almost nothing at 5 analytic fps, so
#: multi-frame fusion was dead code), and fusion now chooses its alignment per read
#: instead of always right-aligning. All three change what a read comes out as.
ANPR_VERSION = "anpr-2.0.0"

MODEL_VERSION = (
    f"{ANPR_VERSION} (open-image-models {DETECTOR_MODEL} + fast-plate-ocr {RECOGNISER_MODEL})"
)

__all__ = ["DETECTOR_MODEL", "RECOGNISER_MODEL", "ANPR_VERSION", "MODEL_VERSION"]
