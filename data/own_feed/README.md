# Own-feed footage

The submission requires a demonstration on our **own** camera feed, separate from the
Government-provided gateway. Drop video files here (`.mp4`, `.mkv`, `.avi`).

`FileSource` replays each file at real-time pace using the container's real PTS, and
loops with a `SCENE_DISCONTINUITY` at the wrap point, so the pipeline sees exactly the
conditions §2.2 describes for the gateway feed. This is deliberate: the same ingest code
path serves both, which is what lets one demonstration validate the other.

Files here are **not committed** — see .gitignore. They are footage, not source.

Requirements for a useful demo clip:
- Contains readable Indian number plates at some point (this is what ANPR is measured on)
- At least a few minutes long, so the loop point is exercised
- Ideally the same vehicle appearing more than once, so route reconstruction has hops
