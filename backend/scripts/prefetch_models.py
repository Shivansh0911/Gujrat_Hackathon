#!/usr/bin/env python
"""Download the ANPR model weights into the image at build time.

A cold container otherwise stalls for roughly 15 MB on its first request, which on a
hosted demo means a judge clicking the URL waits on a spinner.

Each model is fetched and reported separately, with retries. An earlier version of
this ran both in one shell expression wrapped in `|| echo skipped`, so when the
detector cached and the recogniser hit a transient SSL error the build still printed
"models cached" and shipped an image with half a cache. The failure then surfaced at
runtime, inside the demo ingest, as an SSL traceback several layers from its cause.
"""

from __future__ import annotations

import time
from pathlib import Path

ATTEMPTS = 3
BACKOFF_S = 5


def fetch(label: str, load) -> bool:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            load()
            print(f"  {label}: cached")
            return True
        except Exception as exc:
            print(f"  {label}: attempt {attempt}/{ATTEMPTS} failed — "
                  f"{type(exc).__name__}: {exc}")
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_S)
    return False


def main() -> int:
    def detector() -> None:
        from open_image_models import LicensePlateDetector

        LicensePlateDetector(detection_model="yolo-v9-t-384-license-plate-end2end")

    def recogniser() -> None:
        from fast_plate_ocr import LicensePlateRecognizer

        LicensePlateRecognizer("cct-s-v1-global-model")

    print("prefetching ANPR models...")
    ok = [fetch("detector", detector), fetch("recogniser", recogniser)]

    cache = Path.home() / ".cache"
    found = sorted(cache.rglob("*.onnx")) if cache.exists() else []
    for path in found:
        print(f"  on disk: {path}")

    if not all(ok):
        # Non-fatal by design: an image without the cache still works, it is just
        # slower on the first request. But the build log must say so plainly.
        print("WARNING: one or more models were not cached at build time")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
