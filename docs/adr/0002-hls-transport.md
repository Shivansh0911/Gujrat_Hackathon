# ADR 0002 — HLS is the default ingest transport; RTSP is an optimisation

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** SETU engineering

## Context

The organiser's integration guide names **RTSP over TCP** as the intended path for AI
inference and describes HLS as a fallback "for dashboards, mobile, restricted networks".
Our pipelines were built accordingly.

Measurement contradicted the guide. On 2026-08-25:

- `live.corp8.cloud` resolves to **172.67.213.199**, a Cloudflare edge address.
- TCP connect to **:443 succeeds**; **:8554 (RTSP) and :8889 (WHEP) both fail**.
- Cloudflare's proxy forwards HTTP(S) ports only. Ports 8554 and 8889 never reach the
  origin, for any client on any network.

This is a property of how the gateway is published, not of our network. See
`docs/SUPPORT_QUERY.md`, which asks the organisers to confirm whether a direct origin
endpoint exists.

Consuming the HLS endpoint then turned out to be non-trivial. Every request is gated on
a **`cookieCheck=1` query parameter**; a cookie alone does not satisfy it. FFmpeg takes
the variant URI from the master playlist verbatim, dropping the parameter, so its
segment fetches hit a redirect and stall until the socket times out at 30 s. The symptom
is `Error when loading first segment`, which reads as a decoder fault rather than an
authorisation failure.

## Decision

**HLS is the default ingest transport.** RTSP is attempted first, per camera, and used
when reachable; otherwise the pipeline falls back to HLS automatically. Transport
selection is a runtime probe result recorded per camera, never an assumption.

Three mechanisms make HLS ingest work:

1. The master playlist is resolved by us, not FFmpeg. We re-append `cookieCheck=1` to
   the variant URI and hand FFmpeg the **variant** playlist, whose segment URIs then
   already carry the parameter.
2. `StreamSession` takes a **URL provider**, not a URL. The variant carries a per-client
   `session` UUID over a live window of roughly six 1.04 s segments, so it must be
   re-resolved on every reconnect.
3. `live_start_index` is pinned to the newest segment. libav's default start point
   (three segments back) can expire before it is fetched on a slow join.

RTSP support is retained in full — TCP forced, same session semantics — because on the
Grand Finale network, or against a real departmental VMS reached over a private link, it
will be available and is the better path.

## Rationale

**Designing for the transport that actually works is not a compromise.** A pipeline
hard-wired to RTSP would have failed completely on the evaluation feed, and the failure
would have looked like a decoder bug rather than a network topology fact.

**Per-camera probing is the interoperability claim made concrete.** The state's estate
mixes analog encoders, IP cameras and several VMS platforms. A platform that discovers
how to reach each source rather than assuming is the same design that lets one adapter
interface span that estate.

**HLS latency is a real cost and it is bounded.** Segment-based delivery adds roughly one
to three seconds over RTSP. For ANPR, cross-camera correlation and route reconstruction —
all of which key off stream PTS mapped to a common timeline, never arrival time — added
transport latency shifts when we learn about an event, not when the event is recorded to
have happened. Route reconstruction is unaffected. Live-wall responsiveness is affected,
and that is the trade we are accepting, visibly, until RTSP is available.

## Consequences

- Time-to-first-frame includes master-playlist resolution: measured 0.05–0.08 s warm,
  longer on the first capture in a process, which pays cold TLS. Join timeout is
  configured at 20 s to accommodate it.
- Every reconnect costs an extra HTTP round trip to re-resolve the variant.
- The gateway intermittently resets new connections (`WinError 10054`) when sessions are
  opened back to back, so ingest paces its own connects.
- The media plane can fail independently of the control plane. On 2026-08-25T16:39Z the
  catalogue returned 200 with all 30 cameras flagged `live: true` while **every**
  playlist returned 502. **A camera's `live` flag is a claim, not a health signal**;
  registry health is derived from probing, never from the catalogue.
- If the organisers expose a direct origin, transport flips to RTSP by configuration and
  probe result, with no change to any pipeline.


---

## Addendum — 2026-09-02: RTSP became reachable, and is now preferred

The decision above rested on a measurement: port 8554 was unreachable because the gateway
resolved to a Cloudflare edge, and Cloudflare proxies 443/80 only. That measurement was
correct and is no longer true.

The organiser moved the estate to `cctv.corp8.cloud` with the media plane on a bare public
IP (`103.250.160.189`) precisely because a CDN cannot carry RTSP or WebRTC. Port 8554 is
open, and the pipeline decodes frames over forced TCP with a join latency of 0.12 s.

**Nothing in the transport layer had to change.** `select_transport` already probed the
port and preferred RTSP when it answered, falling back to HLS when it did not -- the
fallback was the contingency, not the design. What changed is which branch runs. Two
consequences are worth recording:

* **The feed contract's check 1 is now demonstrable.** "Every client forces RTSP over
  TCP" had only ever been proved by static analysis, because there was no reachable port
  to prove it against. It now passes `static+live`.
* **HLS has become the weaker fallback, not the stronger one.** On this estate the
  playlist is behind a login. A client with a session cookie can fetch it; FFmpeg, handed
  only a URL, cannot. So the fallback this ADR chose as the reliable path is now the one
  that needs credentials it cannot carry, and RTSP -- which needs none -- is the reliable
  one. If HLS is ever needed for ingest here, the session cookie has to reach FFmpeg
  through its `headers` capture option. That is not built: the media plane was down when
  this was written and it could not have been verified.

The original decision stands as correct for the estate it was made against. This addendum
records that the estate changed, not that the reasoning was wrong.
