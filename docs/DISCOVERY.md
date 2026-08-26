# Gateway discovery record — 2026-08-25

What the live gateway actually returns, as opposed to what the integration guide
describes. Recorded because several differences change the architecture, and because a
jury asking "did you verify your assumptions?" should get a dated answer.

Source: `GET https://live.corp8.cloud/api/ingest` — HTTP 200, 9,845 bytes.

## Estate

**30 cameras, `id` "1".."30", all `live: true`.** The brief anticipates ~50 on
evaluation day and the homepage says 30+; the count is treated as variable and
rediscovered on every poll. Nothing in the platform holds a camera count.

Locations span at least eight districts — Ahmedabad (Chimanbhai Bridge, Janpath,
Paldi, Visat, C.N. Vidyalaya), Junagadh (Timbavadi, Majewadi, Dolatpara, Char Chowk),
Gir Somnath, Adalaj, Rajkot (incl. bus port), Navsari/Gandevi, Patan, Banaskantha,
Bilimora, Gandhidham — roughly a 500 km spread. This is genuinely dispersed, which is
what makes cross-camera route reconstruction meaningful rather than a demo trick.

## Finding 1 — the catalogue does not know its own stream properties

**20 of the 30 cameras report `codec: ""`, `width: 0`, `height: 0`, `fps: 0.0`,
`bitrate_kbps: 0`.** Only ids 6, 13, 14, 15, 16, 17, 22, 23, 26, 27, 29 carry real
values.

Consequences, all implemented in M0:

- `parse_catalogue` normalises every zero to `None`. A width of 0 is *unknown*, not a
  measurement; propagating it would let a downstream batcher size a tensor to nothing
  and fail a long way from the cause.
- `CameraDescriptor.properties_known` marks which cameras must be probed.
- `backend/scripts/probe_catalogue.py` establishes codec, resolution and **`measured_fps` from
  PTS deltas** for every camera, and writes a report contrasting declared with measured.

This is the organiser's own "never trust the reported frame rate" warning appearing as
literal missing data on day one, not a hypothetical.

## Finding 2 — declared properties, where present, are mixed

Among the ten cameras that do declare: codecs are both `h264` and `hevc`; resolutions
are 1920×1080, 1280×720, 1280×960 and 2560×1440; declared FPS ranges 12.5 → 25.0.

Confirms §2.2's "never assume a uniform grid" concretely: a single fixed-shape
inference batch across the estate is impossible. Cameras are grouped into batch
cohorts by (resolution, codec).

## Finding 3 — WHEP is advertised over plain HTTP

The catalogue returns `webrtc_url: "http://live.corp8.cloud:8889/stream/<id>/whep"` —
**`http`, not `https` as §2.1 of the brief states.** The console is served over HTTPS,
so a browser will block this as mixed content.

This turns the MediaMTX re-publishing layer (§4) from a security nicety into a
functional requirement: the live wall cannot consume the upstream WHEP URL directly
from an HTTPS page. It happens to be the same design that keeps upstream URLs and
credentials out of the browser, so the security argument and the practical constraint
point the same way.

## Finding 4 — HLS is a relative path

`hls_live_url` is `/live/stream/<id>/index.m3u8`, with no scheme or host. Resolved
against the configured gateway via `urljoin` rather than assumed, so a Phase 2 gateway
on a different scheme or port needs no code change.

## Finding 5 — no coordinates anywhere

The catalogue carries only a free-text `location` string. There is **no latitude or
longitude**, so the GIS layer and every spatio-temporal plausibility check in route
reconstruction depend on coordinates the platform must obtain elsewhere.

Handled by `data/seed/camera_geo.csv` — 30 rows, every one currently
`geom_source=unset` with null coordinates, to be filled by the team from a map.
Coordinates are never inferred or generated. See `data/seed/README.md` for how
`confidence_radius_m` widens route plausibility tolerance and why `unset` cameras are
excluded from route reconstruction *visibly* rather than silently.

## Finding 6 — RTSP and WHEP are unreachable; HLS is the only viable transport

`live.corp8.cloud` resolves to **172.67.213.199, a Cloudflare edge**. Cloudflare proxies
80/443 only, so ports 8554 (RTSP) and 8889 (WHEP) never reach the origin. Verified by
TCP connect: 443 succeeds, 8554 and 8889 both fail. This is not a local firewall — it
is a property of how the gateway is published.

§2.2 anticipates exactly this ("if port 8554 is blocked on the network, fall back to
the HLS endpoint"), so `backend/services/common/transport.py` decides transport per camera by
probing, never by assumption. RTSP is still attempted first and TCP is still forced,
because on the Grand Finale network or against a departmental VMS it will be available
and is the better path.

### The HLS quirk that makes this non-trivial

Every HLS request is gated on a **`cookieCheck=1` query parameter**. A cookie does not
satisfy it — a master-playlist request carrying only the cookie still returns 302.
FFmpeg takes the variant URI from the master playlist verbatim, dropping the parameter,
so its segment fetches hit the redirect and stall until the socket times out at 30 s.
The symptom is `Error when loading first segment`, which reads as a decoder fault
rather than an auth failure, and cost real debugging time to localise.

Resolution: fetch the master ourselves, re-append `cookieCheck=1` to the variant URI,
and hand FFmpeg the **variant** playlist. Segment URIs inside it then already carry the
parameter. Confirmed working — 60 frames of 1920×1080 HEVC decoded, **measured 24.18 fps
against a declared 25.0**, PTS stepping cleanly at 40 ms.

Two further consequences, both handled:

- The variant URI carries a per-client `session` UUID over a live window of only ~6
  seconds of 1.04 s segments. It **must be re-resolved on every reconnect**, which is
  why `StreamSession` takes a URL *provider* rather than a URL.
- The gateway intermittently resets new connections (`WinError 10054`) when sessions
  are opened back to back, and `live_start_index` must be pinned to the newest segment
  because libav's default start point (three segments back) can expire before it is
  fetched.

## Finding 7 — a decoder fault can arrive as a C++ exception, not a return value

`cv2.VideoCapture.read()` raised `cv2.error: Unknown C++ exception from OpenCV code`
rather than returning `False`, and it propagated out of the frame generator and killed
the process. In production that would permanently take out the ingest worker for a
camera. `StreamSession` now treats it as a read failure and reconnects.

Related: releasing a `VideoCapture` from another thread while a read is in flight is
undefined behaviour in OpenCV — it was the trigger here. Cross-thread control of a
session is therefore a flag honoured by the reading thread (`request_reconnect()`),
never a release. That control also backs the Health screen's per-camera reset action.

## Finding 8 — scene-cut detection cannot require both signals

The first detector declared a cut only when histogram correlation collapsed **and**
mean absolute difference spiked. Against the live feed it missed a hard cut between two
genuinely different Gujarat road scenes: `hist_corr=0.67, mad=62.1`.

Real CCTV road scenes share a global brightness distribution — tarmac, sky and
streetlight occupy similar proportions of any road camera's histogram — so correlation
stays high even when content is completely different. Pixel movement is the reliable
signal; the histogram is corroboration for the marginal band only. Retuned to declare a
cut on overwhelming pixel change alone, or on both signals agreeing. Now detected at
`hist_corr=0.48, mad=62.7`, with zero false positives across 40 consecutive frames.

## Finding 9 — the catalogue's `live` flag is a claim, not a health signal

**2026-08-25T16:31:46Z — 16:47:00Z (ongoing at time of writing).**

`GET /api/ingest` returned **HTTP 200 with all 30 cameras flagged `live: true`**, while
**every** `GET /live/stream/<id>/index.m3u8?cookieCheck=1` returned **HTTP 502 Bad
Gateway**, and later stopped responding altogether. Reproduced independently with
`curl 8.7.1` and with OpenCV 4.10.0/FFmpeg. The same code decoded frames from these
cameras at 05:31Z the same day, so this is gateway state, not a regression on our side.

The control plane and the media plane fail independently. This is not a quirk of one
gateway — it is the normal condition of any federated estate, where an inventory record
asserts a camera exists and only a probe establishes that it works.

Consequences now baked into the design:

- **Registry health is derived from probing, never from the catalogue.** `camera.live`
  as reported upstream is stored as a declared attribute alongside `health_state`, in
  exactly the way `declared_fps` sits alongside `measured_fps`.
- A camera advertised as live but not servable transitions to `DEGRADED`/`UNREACHABLE`
  on probe evidence, and the divergence between declared and observed is itself a
  reportable signal on the Health screen.
- `backend/scripts/await_gateway.py` treats readiness as "a playlist actually served", not "the
  catalogue says live".

Raised with the organisers in `docs/SUPPORT_QUERY.md` (Issue 2), since the divergence
may be masking the fault from their own monitoring.

## Finding 10 — RTSP/WHEP unreachability re-measured and confirmed

**2026-08-25T16:46Z.** Re-verified with fresh DNS and TCP evidence for the support query:

```
live.corp8.cloud -> 104.21.59.42, 172.67.213.199   (both Cloudflare ranges)
  :443  OPEN      :80   OPEN
  :8554 TIMEOUT   :8889 TIMEOUT
```

Two Cloudflare edge addresses, not one. HTTP(S) ports open, media ports closed, from a
connection with no outbound filtering. This is how the gateway is published, not a local
firewall — recorded as ADR 0002, and the question of whether a direct origin exists is
put to the organisers in `docs/SUPPORT_QUERY.md` (Issue 1).

## Local evaluation hardware

Python 3.12.5, Docker 29.7.2, Node 20.14, **NVIDIA RTX 4050 Laptop, 6 GB VRAM**.

6 GB is the binding constraint on the analytics plane and is why the adaptive sampling
controller (§7.2) is load-bearing rather than a bonus: 30 cameras at full frame rate
will not fit, and the honest answer to "how do you reach 80,000?" is spending compute
where the evidence is, not assuming a larger GPU.
