import numpy as np

from services.common.scene_cut import SceneCutDetector


def _scene(seed: int, brightness: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 60, (240, 320, 3), dtype=np.uint8)
    return np.clip(base.astype(np.int16) + brightness, 0, 255).astype(np.uint8)


def _feed(det: SceneCutDetector, frame: np.ndarray, n: int) -> int:
    return sum(1 for _ in range(n) if det.update(frame))


def test_static_scene_never_reports_a_cut():
    det = SceneCutDetector()
    assert _feed(det, _scene(1, 40), 30) == 0


def test_moving_object_does_not_report_a_cut():
    det = SceneCutDetector()
    base = _scene(2, 60)
    cuts = 0
    for x in range(0, 200, 8):
        frame = base.copy()
        frame[100:140, x : x + 40] = 220  # a vehicle crossing the frame
        if det.update(frame):
            cuts += 1
    # Few pixels change, so MAD stays low even though the histogram shifts.
    assert cuts == 0


def test_hard_cut_to_an_unrelated_scene_is_detected():
    det = SceneCutDetector()
    _feed(det, _scene(3, 30), 10)
    assert det.update(_scene(99, 200)) is True


def test_exposure_ramp_alone_does_not_trip_the_detector():
    # Auto-exposure and headlight glare move the histogram without moving content;
    # a single-signal detector would reset trackers every dusk.
    det = SceneCutDetector()
    base = _scene(4, 20)
    det.update(base)
    cuts = 0
    for b in range(0, 60, 5):
        frame = np.clip(base.astype(np.int16) + b, 0, 255).astype(np.uint8)
        if det.update(frame):
            cuts += 1
    assert cuts == 0


def test_high_histogram_correlation_does_not_mask_a_real_cut():
    """Regression: the live feed's two road scenes correlated at 0.67 with mad=62.

    Requiring both signals unconditionally missed that cut. Real CCTV road scenes share
    a global brightness distribution, so strong pixel movement must stand on its own.
    """
    # Identical histograms by construction (each is half dark, half bright), but
    # completely different spatial structure -- the split runs vertically in one and
    # horizontally in the other.
    a = np.full((240, 320, 3), 30, np.uint8)
    a[:, :160] = 210
    b = np.full((240, 320, 3), 30, np.uint8)
    b[:120, :] = 210

    det = SceneCutDetector()
    _feed(det, a, 6)
    cut = det.update(b)

    assert det.last_corr is not None and det.last_corr > 0.55  # histogram says "same"
    assert det.last_mad is not None and det.last_mad > 50.0  # pixels say "different"
    assert cut is True


def test_reset_suppresses_an_immediate_second_cut():
    det = SceneCutDetector()
    _feed(det, _scene(5, 30), 10)
    assert det.update(_scene(77, 210)) is True
    # Warm-up: the first frames of a new segment are often partial keyframes and
    # would otherwise trip the detector again immediately.
    assert det.update(_scene(5, 30)) is False


def test_greyscale_input_is_accepted():
    det = SceneCutDetector()
    grey = np.full((240, 320), 50, np.uint8)
    assert det.update(grey) is False
