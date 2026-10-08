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

#: Length of the appearance fingerprint stored in `vehicle_sighting.appearance_vector`.
#:
#: Declared here, in the module that imports nothing, for the same reason the model
#: names are: `services.registry.models` needs it to size the pgvector column, and
#: reaching into `services.analytics.appearance` to ask would drag OpenCV into the API
#: process at import time. `appearance.py` imports this value rather than defining its
#: own, so the column width and the descriptor cannot drift apart.
#:
#: 64 chroma bins + 4 lightness bins + 8 edge-orientation bins. Changing it requires a
#: migration and a new APPEARANCE_KIND -- stored vectors of the old width are not
#: comparable and must not be silently reinterpreted.
APPEARANCE_DIM = 76

#: Stamped on every stored vector so two layouts are never compared. See
#: `services.analytics.appearance` for the layout itself and why it is weighted.
#:
#: v1 was a 4x4x4 joint CIELAB cube. It measured a relit crop of one vehicle as further
#: from itself than from a different vehicle, because lightness occupied a third of the
#: cube. Nothing was ever persisted under it; the name is burnt rather than reused.
APPEARANCE_KIND = "ab64-l4-edge8-v2"

#: Vehicle detector. Apache-2.0, from Megvii's own release.
#:
#: Ultralytics YOLO is AGPL and excluded (ADR 0003), which rules out the obvious choice.
#: YOLOX-tiny is the strongest small-object detector available under a permissive
#: licence at a size a free-tier CPU instance can run. See
#: `services.analytics.vehicle_detect` for what was considered and rejected.
VEHICLE_DETECTOR_MODEL = "yolox_tiny"
VEHICLE_DETECTOR_INPUT = 416
VEHICLE_DETECTOR_URL = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"
)

#: Verified on every load, not only on download. A model file decides what the platform
#: reports having seen, so a substituted or truncated one must fail loudly rather than
#: quietly change the evidence.
VEHICLE_DETECTOR_SHA256 = "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"

#: Bumped whenever vehicle detection, the appearance descriptor or the candidate scorer
#: changes in a way that could alter which sightings are linked. Printed alongside
#: ANPR_VERSION on evidence exports so a disputed journey can be tied to the exact
#: analytics that produced it.
SIGHTING_VERSION = "sighting-1.0.0"

__all__ = [
    "ANPR_VERSION",
    "APPEARANCE_DIM",
    "APPEARANCE_KIND",
    "DETECTOR_MODEL",
    "MODEL_VERSION",
    "RECOGNISER_MODEL",
    "SIGHTING_VERSION",
    "VEHICLE_DETECTOR_INPUT",
    "VEHICLE_DETECTOR_MODEL",
    "VEHICLE_DETECTOR_SHA256",
    "VEHICLE_DETECTOR_URL",
]
