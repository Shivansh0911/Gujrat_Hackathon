# ADR 0003 — ANPR models are chosen by licence first, accuracy second

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** SETU engineering
- **Relates to:** the licence rule in the build instructions — Apache-2.0 / MIT / BSD
  preferred, AGPL isolated behind our own interface with an ADR

## Context

Automatic number plate recognition is the scored capability. The obvious choice for
plate detection is a YOLO model from Ultralytics: it is the best-documented option, the
pretrained weights are easy to obtain, and almost every tutorial on the subject uses it.

Ultralytics is **AGPL-3.0**. The AGPL's network clause is the problem, not the
copyleft: a platform that is *used over a network* triggers the obligation to offer
corresponding source to its users. SETU is exactly that — a web platform whose users
are police officers in an operational deployment. A commercial licence from Ultralytics
exists, but committing a state police procurement to a per-deployment negotiation with
a single vendor is a worse outcome than choosing a different model.

This decision is not hypothetical for this jury. The submission is evaluated partly on
maturity and PoC readiness, and "we would need to buy a licence before you could deploy
this" is a material finding for anyone assessing whether the work can actually be
operationalised.

## Decision

Both models in the ANPR pipeline are **MIT**:

| Role | Model | Package | Licence |
|---|---|---|---|
| Plate detection | YOLOv9-t, 384px, end-to-end ONNX | `open-image-models` | MIT |
| Plate recognition | CCT (compact convolutional transformer) ONNX | `fast-plate-ocr` | MIT |

Both ship as ONNX and run on `onnxruntime`, so there is no framework lock-in and no GPU
requirement — which is what makes the CPU-only throughput benchmark meaningful and the
edge-deployment argument credible.

Ultralytics is **not a dependency at any tier**, including development and test. A
transitive reintroduction would silently re-arm the obligation, so the constraint is
enforced by absence rather than by policy.

## The interface that makes this reversible

The pipeline depends on two narrow protocols, not on either package:

```python
class PlateDetector(Protocol):
    def detect(self, image) -> list[tuple[Box, float]]: ...

class PlateRecogniser(Protocol):
    def read(self, crop) -> tuple[str, list[float]]: ...
```

`OpenImagePlateDetector` and `FastPlateRecogniser` are implementations. Swapping in a
different model — including an AGPL one behind a licence, or a commercial ANPR SDK a
department already owns — is a new class, not a change to the pipeline. This is the
same structural move as ADR 0001 and the `CameraSource` protocol: the thing likely to
be replaced sits behind an interface owned by us.

That matters beyond licensing. A department with an existing ANPR investment does not
want to be told to discard it, and the federation argument in the README ("video stays
at the edge; metadata flows to the centre") only holds if the recogniser at the edge
can be somebody else's.

## Consequences

**Accepted:** these models are less accurate than the best available proprietary Indian
ANPR engines, and materially less accurate at low resolution — the same clip yields 8
grammar-valid plates at 2560×1440, 2 at 1280×720 and 0 at 704×396. That resolution
sensitivity is documented in `docs/HLD_RECONCILIATION.md` and is the reason the
sub-stream scalability figure had to be withdrawn.

**Gained:** the platform can be deployed by any department without a licence
negotiation, the throughput numbers are honest CPU numbers, and the recogniser is a
replaceable component rather than an architectural commitment.

**Outstanding:** precision and recall for these specific models on Indian plates are
measured in `data/seed/anpr_ground_truth.csv` and
`reports/evidence/anpr-accuracy-*.md`. Any future model swap must be re-scored against
the same annotation sheet, or the comparison is not a comparison.
