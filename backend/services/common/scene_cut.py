"""Scene discontinuity detection for the recording loop point (§2.2).

Each feed is a ~12h recording replayed on a loop. At the wrap the scene cuts hard,
exactly like a camera reboot. Anything holding long-lived state -- background models,
re-identification galleries, tracker IDs -- must notice and reset, or it will happily
associate the last vehicle before the cut with the first vehicle after it and emit a
journey hop that never happened.

The detector deliberately uses only cheap global statistics on a downscaled greyscale
frame: it runs on every camera at full frame rate, so it must cost far less than
detection. Two signals are computed, because a false positive resets trackers and
loses in-flight associations:

  1. Histogram correlation collapse -- the global brightness distribution changed.
  2. Mean absolute difference spike -- the pixels actually moved, not just exposure.

A cut is declared when the pixel evidence is overwhelming on its own, or when both
signals agree. Requiring both *unconditionally* was the first design and it failed
against the live feed: two entirely different Gujarat road scenes still correlated at
0.67, because tarmac, sky and streetlight occupy similar proportions of any road
camera's histogram. Pixel movement is the reliable signal; the histogram is corroboration
for the marginal band.

A vehicle crossing the frame moves few enough pixels to fail both branches; an
auto-exposure ramp or headlight glare shifts the histogram while barely moving MAD.

This detector is the *backup* signal. The primary indicator of the recording loop point
is a PTS wrap, handled in StreamSession, which catches the case regardless of content.
"""

from __future__ import annotations

import numpy as np

from services.common.cv_env import cv2

# Downscale target. Small enough to be nearly free, large enough that a cut between
# two visually similar road scenes still registers.
_WORK_W, _WORK_H = 64, 64

# Histogram correlation below this means the distributions are largely unrelated.
_HIST_CORR_THRESHOLD = 0.55

# Mean absolute difference (0-255 scale) above this means most pixels changed.
_MAD_THRESHOLD = 38.0

# MAD above which the pixel evidence alone is conclusive, regardless of histogram.
#
# Tuned against the live feed, not guessed: a hard cut between two genuinely different
# Gujarat road scenes (camera 1 -> camera 6) measured hist_corr=0.67 with mad=62.1.
# Requiring BOTH signals missed it, because real CCTV road scenes share a global
# brightness distribution -- tarmac, sky and streetlight at similar proportions -- so
# histogram correlation stays high even when the content is completely different.
# Nothing short of a scene replacement moves this many pixels: a vehicle crossing the
# frame and an exposure ramp both measure far below it (see tests).
_MAD_STRONG_THRESHOLD = 50.0

# Frames to ignore after a reset. The first frames of a new segment are often a
# partially-reconstructed keyframe, which would otherwise trip the detector again.
_WARMUP_FRAMES = 3


class SceneCutDetector:
    """Stateful per-camera detector. Call `update()` with each decoded frame."""

    def __init__(
        self,
        hist_corr_threshold: float = _HIST_CORR_THRESHOLD,
        mad_threshold: float = _MAD_THRESHOLD,
        mad_strong_threshold: float = _MAD_STRONG_THRESHOLD,
    ) -> None:
        self._hist_corr_threshold = hist_corr_threshold
        self._mad_threshold = mad_threshold
        self._mad_strong_threshold = mad_strong_threshold
        self._prev_small: np.ndarray | None = None
        self._prev_hist: np.ndarray | None = None
        self._warmup = 0
        self.last_corr: float | None = None
        self.last_mad: float | None = None

    def reset(self) -> None:
        """Forget history. Called on reconnect and after a declared cut."""
        self._prev_small = None
        self._prev_hist = None
        self._warmup = _WARMUP_FRAMES

    @staticmethod
    def _prepare(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        grey = (
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if frame.ndim == 3
            else frame
        )
        small = cv2.resize(grey, (_WORK_W, _WORK_H), interpolation=cv2.INTER_AREA)
        hist = cv2.calcHist([small], [0], None, [64], [0, 256])
        cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
        return small, hist

    def update(self, frame: np.ndarray) -> bool:
        """Return True when this frame begins a new scene."""
        small, hist = self._prepare(frame)

        if self._prev_small is None or self._prev_hist is None:
            self._prev_small, self._prev_hist = small, hist
            return False

        if self._warmup > 0:
            self._warmup -= 1
            self._prev_small, self._prev_hist = small, hist
            return False

        corr = float(cv2.compareHist(self._prev_hist, hist, cv2.HISTCMP_CORREL))
        mad = float(np.mean(cv2.absdiff(self._prev_small, small)))
        self.last_corr, self.last_mad = corr, mad

        self._prev_small, self._prev_hist = small, hist

        # Overwhelming pixel change is conclusive on its own; otherwise both signals
        # must agree. See the threshold comments for the live measurements behind this.
        strong = mad > self._mad_strong_threshold
        corroborated = mad > self._mad_threshold and corr < self._hist_corr_threshold
        if strong or corroborated:
            self._warmup = _WARMUP_FRAMES
            return True
        return False
