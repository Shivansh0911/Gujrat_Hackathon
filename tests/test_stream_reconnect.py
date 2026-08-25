"""The reconnect control must be safe to call from another thread."""

import threading

from services.common.stream_client import StreamSession


def _session() -> StreamSession:
    return StreamSession("rtsp://unused.test/stream/1", "1", detect_scene_cuts=False)


def test_request_reconnect_sets_a_flag_and_does_not_touch_the_capture():
    s = _session()
    s._cap = object()  # sentinel: a real capture must never be released cross-thread
    s.request_reconnect()
    assert s._reconnect_requested.is_set()
    # Releasing a VideoCapture under an in-flight read() is undefined behaviour in
    # OpenCV and raised 'Unknown C++ exception' in practice; the flag exists so the
    # reading thread performs the teardown itself.
    assert s._cap is not None


def test_request_reconnect_is_safe_from_many_threads():
    s = _session()
    threads = [threading.Thread(target=s.request_reconnect) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert s._reconnect_requested.is_set()


def test_reconnect_request_is_independent_of_stop():
    s = _session()
    s.request_reconnect()
    # A reset must not end the session -- the worker rejoins rather than exiting.
    assert not s._stop.is_set()
    s.stop()
    assert s._stop.is_set()
