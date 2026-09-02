# Support query — catalogue fields, and media-plane throughput

**To:** Gujarat Police Innovation Challenge 2026 — CCTV Integration Hackathon, feed support
**From:** Team SETU (Category 1 — student team)
**Raised:** 2026-09-02T16:30:00Z
**Estate:** `cctv.corp8.cloud` (catalogue and HLS) and `103.250.160.189` (RTSP :8554)
**Format:** per the integration guide's support protocol (§5) — camera id, exact URL,
client and version, UTC timestamp, client-side error log.

**Client:** Python 3.12, `requests` 2.32 and OpenCV/FFmpeg, RTSP forced over TCP.

Neither item below is blocking us. Both are places where the estate's behaviour and the
integration guide disagree, and other teams are likely hitting them without knowing why.

Our two earlier issues, raised 2026-08-25 against the previous estate, are **resolved**
and kept at the end of this document for the record. The second of them turned out to be
our own fault, and we say so there.

---

## Issue 1 — `/api/ingest` returns only `id` and `name`

### What the guide says

> It returns every camera with its id, location, codec, live status, stream properties,
> and all three URLs.

and, under *Don't assume a uniform grid*:

> Cameras differ in resolution, codec, frame rate, and bitrate. Read per-camera
> properties from `/api/ingest` and size batching, buffers, and decoders accordingly.

The pre-submission checklist repeats it: *"Camera list and per-camera properties are read
from `/api/ingest`."*

### What the endpoint returns

`GET https://cctv.corp8.cloud/api/ingest` at 2026-09-02T16:12Z — **HTTP 200,
`application/json`, 1,373 bytes for 30 cameras.** The first element in full:

```json
{ "id": "cam01", "name": "01 Chiman bhai Bridge" }
```

The union of keys across all thirty cameras is exactly `["id", "name"]`. There is no
`location`, no `codec`, no `live`, no stream properties and no URLs.

### Why we are raising it

A participant who follows the checklist literally cannot pass it, and a participant who
sizes decoders from catalogue properties will size them from nothing. We have worked
around it by measuring each camera's codec, resolution and frame rate from the decoded
stream and storing that in our registry, which we would argue is the right behaviour
anyway — but it is not what the guide describes, and the difference is large enough that
we assume the endpoint is not serving what you intended.

**Question:** is the thin catalogue intentional for this estate, or has the richer
document been lost in the move from the previous host?

---

## Issue 2 — the HLS plane appears to be throttled per connection (~5 KB/s)

### Measurement

Direct from a workstation, no proxy in the path, authenticated session, browser
user-agent. 2026-09-02T15:40Z–16:05Z.

| Request | Size | Time |
|---|---:|---:|
| `GET /cam01/index.m3u8` | 211 KB | **25 s** |
| `GET /cam01/seg00000.ts` (8 s of video) | 263 KB | **46 s** |
| six segments **sequentially** | 1.2 MB | **277 s** |
| the same six **in parallel** | 1.2 MB | **46 s** |

Sequential throughput is about 5 KB/s per connection. Fetching in parallel multiplies it
almost exactly by the number of connections, which is what makes us think the limit is
per connection rather than per client or per camera.

### Why it matters to participants

`hls.js` requests fragments strictly in order, one at a time. At 5 KB/s it needs roughly
46 seconds to fetch 8 seconds of video, so an unmodified browser player can never keep
up, and reports `fragLoadTimeOut` on a camera that is serving correctly. Your own
control-room page sets `manifestLoadingTimeOut: 60000` and small buffers, which suggests
this is known.

We have handled it by fetching several segments ahead in parallel and serving the player
from a cache, which brings 48 seconds of video down to about 1 second of wall clock. We
are not asking you to change anything — we would like to know whether the throttle is
deliberate, so we can say so accurately in our submission rather than describing your
infrastructure as slow.

### Camera availability, for the record

Across a full 30-camera sweep on 2026-09-02 (RTSP/TCP, 25–40 s budget each, paced one at
a time per your *pace your load* guidance): **22 produced frames, 8 did not**, each of
the 8 returning no frames within the budget rather than an error. Camera ids:
`cam07`–`cam11`, `cam21`, `cam24`, `cam25`. This may simply be normal supervision
restarts; we mention it only so the figure in our report is not mistaken for a claim that
your estate was down.

---

# Previously raised, 2026-08-25 — now resolved

Kept because a support record that quietly deletes its own history is not a record. Both
were against `live.corp8.cloud`, the estate retired on 2026-09-01.

**Issue 1 (RTSP/WHEP unreachable) — resolved by the move.** Port 8554 on
`103.250.160.189` answers, and our full pipeline now runs over RTSP/TCP against it.

**Issue 2 (all playlists 502) — partly ours.** The estate was genuinely returning
Cloudflare 502s on 2026-08-31. But on the new estate the same symptom in our console
turned out to be our own client: the media plane answers a non-browser user-agent with
`403 browser required`, and because 403 also signals "sign in", our client
re-authenticated and was refused again. Our fault, recorded here so the earlier report is
not left overstating a problem on your side.

