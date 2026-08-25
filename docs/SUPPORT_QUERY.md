# Support query — RTSP/WHEP reachability, and a media-plane 502 outage

**To:** Gujarat Police Innovation Challenge 2026 — CCTV Integration Hackathon, feed support
**From:** Team SETU (Category 1 — student team)
**Raised:** 2026-08-25T16:47:00Z
**Format:** per the integration guide's support protocol (§2.5) — camera id, exact URL,
client and version, UTC timestamp, client-side error log.

We have confirmed each camera's `live` status in `/api/ingest` before reporting, as the
protocol requires. **All 30 cameras are flagged `live: true` in the catalogue.**

There are two separate issues. Issue 1 is a question about intended architecture. Issue 2
is an outage we believe you will want to know about immediately.

---

## Issue 1 — RTSP (:8554) and WHEP (:8889) are not reachable from any network

### Question

The integration guide names RTSP over TCP as the intended path for AI inference
(OpenCV, GStreamer, FFmpeg, DeepStream) and describes HLS as a fallback for dashboards,
mobile and restricted networks. We cannot reach the RTSP or WHEP ports at all.

**Is there a direct origin endpoint for RTSP/WHEP that participants should use, or is
HLS the intended ingest path for all participants?**

We ask because the answer changes what we optimise. If other teams are consuming RTSP
directly, we are demonstrating on materially higher latency for no reason. If HLS is the
intended path for everyone, we would like to state that confidently in our architecture
documentation rather than describe it as a workaround.

### Evidence — DNS

```
$ python -c "import socket; print(sorted({ai[4][0] for ai in socket.getaddrinfo('live.corp8.cloud', None)}))"
['104.21.59.42', '172.67.213.199']
```

Both addresses are in Cloudflare ranges. Cloudflare's reverse proxy forwards HTTP(S)
ports only, which would explain the port behaviour below as a property of how the
gateway is published rather than a local firewall.

### Evidence — port reachability

Measured 2026-08-25T16:46Z, from a residential ISP connection in India with no
outbound port filtering (verified: other hosts' 8554 is reachable from this machine).

```
  :443  OPEN
  :80   OPEN
  :8554 FAILED (TimeoutError after 6s)   <- RTSP
  :8889 FAILED (TimeoutError after 6s)   <- WHEP
```

### Evidence — client-side error log

- **Camera id:** 13 (`13 CN Vidhyalaya`) — behaviour is identical for all 30 cameras
- **Exact URL:** `rtsp://live.corp8.cloud:8554/stream/13`
- **Client:** OpenCV 4.10.0, FFmpeg backend (avcodec 58.134.100, avformat 58.76.100),
  `rtsp_transport;tcp` forced, Python 3.12.5 on Windows 11
- **UTC timestamp:** 2026-08-25T16:46:12Z

```
TCP connect to live.corp8.cloud:8554 -> TimeoutError (no SYN-ACK within 6s)
cv2.VideoCapture(...).isOpened() -> False
```

`ffplay -rtsp_transport tcp rtsp://live.corp8.cloud:8554/stream/13` fails identically at
the TCP layer, before any RTSP exchange.

### What we have done meanwhile

We fall back to HLS automatically, per the guide's own instruction for restricted
networks, and our pipeline is fully operational on it — 1920×1080 HEVC decoding at a
measured 24.18 fps against a declared 25.0, with all eight §2.4 checklist items passing.
No change is required on your side for us to proceed. This question is about whether we
are on the intended path.

### One note that may help other participants

Consuming the HLS endpoint required a step that is not in the integration guide, and we
suspect other teams are losing time on it. Every request is gated on the
**`cookieCheck=1` query parameter** — a cookie alone does not satisfy it. FFmpeg takes
the variant URI from the master playlist verbatim, dropping the parameter, so its segment
requests hit a redirect and stall until the socket times out at 30 s. The visible symptom
is:

```
[hls @ ...] Error when loading first segment
  'https://live.corp8.cloud/live/stream/1/..._seg1518.mp4?cookieCheck=1&session=<uuid>'
```

which reads as a decoder fault rather than an authorisation failure. Resolving the master
playlist client-side and re-appending `cookieCheck=1` to the variant URI resolves it. If
this matches your expectations, a line in the integration guide would likely save several
teams a debugging session.

---

## Issue 2 — All camera playlists returning 502 Bad Gateway (media plane down)

**This is an active outage as of the timestamp on this document.** The control plane is
healthy and reports every camera as live; the media plane is serving 502 for all of them.

### Evidence

```
2026-08-25T16:39:30Z
  GET https://live.corp8.cloud/api/ingest                                -> HTTP 200
       (30 cameras returned, all "live": true)
  GET https://live.corp8.cloud/live/stream/13/index.m3u8?cookieCheck=1   -> HTTP 502
  GET https://live.corp8.cloud/live/stream/6/index.m3u8?cookieCheck=1    -> HTTP 502

2026-08-25T16:47:00Z  (re-checked)
  GET https://live.corp8.cloud/api/ingest                                -> HTTP 200
  GET https://live.corp8.cloud/live/stream/13/index.m3u8?cookieCheck=1   -> timeout (no response)
```

- **Cameras affected:** all 30
- **Client:** curl 8.7.1 (Schannel) and OpenCV 4.10.0/FFmpeg, identical results
- **First observed:** 2026-08-25T16:31:46Z
- **Still failing at:** 2026-08-25T16:47:00Z

### Client-side error log (camera 13, representative of all 30)

```
2026-08-25 16:31:46Z WARNING stream_client: stream URL resolution failed camera=13:
    502 Server Error: Bad Gateway for url:
    https://live.corp8.cloud/live/stream/13/index.m3u8?cookieCheck=1
2026-08-25 16:31:47Z WARNING stream_client: connect failed camera=13 attempt=1 retry_in=1.0s
2026-08-25 16:31:49Z WARNING stream_client: stream URL resolution failed camera=13: 502 ...
2026-08-25 16:31:50Z WARNING stream_client: connect failed camera=13 attempt=2 retry_in=1.0s
[... identical for 12 further attempts across a 23s window, exponential backoff applied ...]
```

The same code path decoded frames successfully from these cameras at
**2026-08-25T05:31Z** the same day, so this is a change in gateway state rather than a
change on our side.

### Impact on us

Low — we retry with exponential backoff and resume automatically when the feed returns.
We are reporting it because the catalogue continues to advertise these cameras as live
while they are not servable, and if that divergence is unintentional it may be masking
the fault from your monitoring.

---

## Contact

Team SETU. Please reply to the address on our submission record. We can supply full
packet-level logs, our client configuration, or run any diagnostic you would find useful.
