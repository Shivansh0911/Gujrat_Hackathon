# Project SETU

Unified CCTV integration, monitoring and video-analytics platform for the Gujarat
Police Innovation Challenge 2026 (CCTV Integration Hackathon), Category 1.

**Solution model:** Hybrid / Innovative Architecture — Model 1 (Centralised CCTV
Registry & GIS Mapping) as an *active control plane*, Model 3 federation as the
backbone, Model 4's analytics and evidence plane, with Model 2 direct-connect
implemented as one adapter type inside the federation layer.

## Status — Milestone 0 complete

Foundation and feed-contract compliance. What exists and works today:

| Component | Purpose |
|---|---|
| `services/common/cv_env.py` | Sole cv2 import point; forces RTSP-over-TCP before the FFmpeg backend reads its options. |
| `services/common/config.py` | Environment-driven settings. No host, port or secret in source. |
| `services/common/redact.py` | Root-logger filter scrubbing credentials and upstream stream URLs. |
| `services/common/catalogue.py` | `/api/ingest` client. Normalises the catalogue's `0`/`""` sentinels to `None`. |
| `services/common/transport.py` | Per-camera transport selection, RTSP→HLS fallback, HLS variant resolution. |
| `services/common/stream_client.py` | `StreamSession`: PTS-only timing, backoff reconnect, join tolerance, measured FPS. |
| `services/common/scene_cut.py` | Two-signal scene-discontinuity detector for the recording loop point. |
| `scripts/probe_catalogue.py` | Measures real codec/resolution/FPS per camera; reconciles against the catalogue. |
| `scripts/preflight_check.py` | Verifies the organiser's §2.4 checklist empirically. CI gate. |
| `data/seed/camera_geo.csv` | Camera coordinates — team-supplied, never inferred. |

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
cp .env.example .env        # then set SETU_GATEWAY_HOST
```

`SETU_GATEWAY_HOST` has no default in source. This is deliberate: Phase 2 adds a
second gateway, and a default would be a hardcoded host by another name.

```bash
.venv/Scripts/python -m pytest tests -q            # unit tests, no network
.venv/Scripts/python scripts/preflight_check.py    # §2.4 checklist against the live feed
.venv/Scripts/python scripts/probe_catalogue.py    # declared-vs-measured stream properties
```

## Two findings that shaped this milestone

Both are recorded with evidence in [`docs/DISCOVERY.md`](docs/DISCOVERY.md).

**The gateway is behind Cloudflare, so RTSP is unreachable.** `live.corp8.cloud`
resolves to a Cloudflare edge, which proxies 443/80 only — ports 8554 (RTSP) and 8889
(WHEP) never reach the origin. §2.2 anticipates exactly this and mandates the HLS
fallback, so transport is decided per camera by probing, never assumed. RTSP is still
attempted first and TCP is still forced, because on the Grand Finale network or a
departmental VMS it will be the better path.

Consuming the HLS feed additionally requires resolving the master playlist to its
variant ourselves while preserving the gateway's `cookieCheck=1` query parameter —
FFmpeg drops it when following the master, and its segment fetches then stall until
the socket times out, which presents as "the decoder is broken" rather than as an auth
problem. `transport.py` documents and absorbs this.

**The catalogue does not know its own stream properties.** 20 of 30 cameras report
`codec: ""`, `0×0` and `fps: 0.0`. The catalogue is authoritative for *which cameras
exist and how to reach them*, and unreliable for *what they contain* — so stream
properties are measured, not read.

## Non-negotiables enforced in code

- **No secrets in source or history.** `.env` is ignored; credentials come from the
  environment. `redact.py` is the backstop for anything that reaches a log.
- **All timing from PTS.** Arrival time is recorded as telemetry only. `CAP_PROP_FPS`
  is read exactly once, on a line marked reference-only, purely to display the
  discrepancy against `measured_fps` — and `preflight_check.py` fails the build if it
  is used anywhere else.
- **TLS verification is never disabled.** No `verify=False`, anywhere, including tests.
- **Coordinates are never invented.** Cameras without a supplied coordinate are
  excluded from route reconstruction and shown as *"coordinate missing"*, never
  silently dropped.

## Licensing

Every dependency is Apache-2.0, MIT, BSD or PSF. No AGPL, SSPL, BUSL or
non-commercial licences. Versions are pinned exactly in `requirements.txt`.
