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

## Finding 11 - the gateway recovered partially, and camera health is per-camera

**2026-08-27.** After roughly two days of 502 on every media playlist, the gateway
returned. It did not return uniformly. Two full ingest passes across the catalogue
(`reports/evidence/gateway-ingest-*.json`) establish:

```
30 catalogued -> 25 produced frames, 5 produced none
  cameras 17, 18   HTTP 500 on the playlist, repeatably, across both passes
  cameras 22, 23, 30   connection timeouts
```

The failure is per-camera and stable, not a gateway-wide outage. `/api/ingest` served
the catalogue throughout. This is the concrete case for the health model in the
platform: a camera's `live` flag is a claim (Finding 9), and the only trustworthy
signal is whether frames arrived just now.

**A caveat on that run, recorded because it changes how one figure reads.** The first
pass was interrupted by the host suspending. Five cameras then failed on local DNS
rather than on anything the gateway did, and one result carries an elapsed time of
19,059 seconds. Re-running only the affected cameras and merging is what produced the
numbers above; `scripts/gateway_report.py` flags any result whose elapsed time exceeds
its budget by more than 2x and excludes it from timing figures. Frame counts and
PTS-derived rates are unaffected -- they never depended on wall clock.

## Finding 12 - the estate publishes at a resolution ANPR cannot read

**2026-08-27.** 9,158 frames decoded across 25 live cameras produced **30 plate
regions and 2 grammar-valid registrations**. Of the evidence crops written, exactly
three contain a plate legible to a human reviewer -- all three of the same vehicle, on
camera 7.

This is the same effect measured on the own-feed clip, where the identical pipeline
reads 8 plates at 2560x1440, 2 at 1280x720 and none at 704x396. It is not a defect in
the recogniser so much as a property of the feed: at the resolution and framing these
cameras publish, the plate occupies too few pixels to survive.

The consequence for the architecture is in `docs/HLD_RECONCILIATION.md`: the
sub-stream throughput figure describes an operating point at which nothing is read, so
the defensible scaling number is the full-resolution one. It argues for processing at
the edge, where full resolution is still available, rather than shipping downscaled
video to a centre.

## Finding 13 - the plate detector fires on burnt-in text, and the grammar catches it

**2026-08-27.** Reviewing every gateway crop by eye showed the detector firing on
things that are not plates but look like them to a box detector -- rectangular regions
of high-contrast characters:

| Camera | What the crop actually contains | OCR output |
|---|---|---|
| 7 | OSD banner: `HERO SHOWROOM FIX-1 / Bhavani Char Rasta to Hero` | `LL450A` |
| 15 | OSD banner: `Suvidhapark P3 RLVD` | `E523BD` |
| 16 | OSD date overlay | `P2506SL` |
| 21 | A lorry's painted name board: `GORSIYA` | `E00033DA` |
| 25 | OSD banner: `GRAM PANCHAYAT 1` | `HHMNNH11` |

**Every one of these is rejected by the Indian plate grammar and never becomes a
registration.** That is the layered design doing its job: the detector is permissive,
the grammar is not, and a false positive has to survive both. Worth stating plainly to
a jury, because it is the difference between a system that reports a camera's own
caption as a vehicle and one that does not.

The cost is wasted OCR invocations, not wrong evidence. A region mask per camera would
recover it, and is recorded as outstanding rather than done.

## Finding 14 - the OSD text names the camera's location

**2026-08-27.** A consequence of Finding 13 worth separating from it. Finding 5 records
that the catalogue carries no coordinates anywhere, which is why 10 cameras sit at
district centroids with an honest confidence radius drawn around them rather than at a
false precise pin.

But the video itself carries location names, burnt in by the camera:

- Camera 7: `Bhavani Char Rasta to Hero` - a named junction in Ahmedabad
- Camera 15: `Suvidhapark P3 RLVD` - a named location with a red-light-violation
  detection unit
- Camera 25: `GRAM PANCHAYAT 1`

This is a real, evidence-backed route to placing cameras that the catalogue cannot
place: read the OSD, geocode the name through the existing cache, and record the
provenance as *derived from on-screen text* so it is distinguishable from a surveyed
coordinate and from a district centroid.

**Not implemented.** It is recorded here rather than acted on because doing it in a
hurry is how a coordinate gets invented, and the rule is that every coordinate traces
to the geocode cache or a named district centroid. Done properly it needs its own
provenance value, its own confidence radius, and a human confirming each match. It is
the highest-value use of the OSD discovery and the natural next piece of work.

## Finding 15 - the recogniser had fewer character slots than an Indian plate

**2026-08-27.** The first end-to-end accuracy measurement returned **0.0% precision
and 0.0% recall**: across every annotated evidence crop, not one registration was read
correctly. The cause was not tuning.

`fast-plate-ocr` models declare a `max_plate_slots` in their config, which is the
number of classification heads the network has. The model we had configured,
`cct-s-v1-global-model`, declares:

```
max_plate_slots: 9
```

An Indian registration under the current scheme is **ten** characters -- `XX00XX0000`,
as in `GJ32AG1111`. A nine-slot model cannot represent one. This is arithmetic, not
accuracy: every full-length Indian plate was wrong before inference began, and no
amount of image quality, tuning or post-processing could have recovered it.

The evidence had been sitting in the output the whole time. **Every single read was
exactly nine characters long.** `KA25AB1542` came back as `KA25AB154`, `GJ14AK5333` as
`GJ14AK533`, `GJ36AR0180` as `GJ36AR018`. A uniform output length across hundreds of
reads is not a plausible property of real number plates, and it should have been the
first thing questioned.

`cct-s-v2-global-model` declares `max_plate_slots: 10` and is now the default, pinned
in `services/analytics/model_ids.py` with the reasoning attached, and guarded by a
test that fails if the configured model reverts to a v1 generation.

**Two further defects surfaced once this one was fixed**, both of which had been
masked by it:

- **Track association never associated anything.** Detections were matched across
  frames by bounding-box overlap alone at a 0.25 IoU threshold. Analytic sampling runs
  at 5 fps, so consecutive looks at a vehicle are 200 ms apart, and in 200 ms a plate
  travels further than its own width -- the boxes overlap by exactly nothing. 22 plate
  detections produced 14 tracks, 13 of them one frame long. **Multi-frame fusion, one
  of the pipeline's headline features, had never once run on real footage.**
- **Fusion voted misaligned characters against each other.** Reads of differing length
  were always right-aligned, on the reasoning that Indian plates end in a numeric
  group. That is correct when OCR drops a *leading* character and wrong when it drops
  a trailing one, and the newer model drops trailing ones. Three near-correct reads of
  `KA25AB1542` fused to `KA25A1154` -- worse than the best single read, because the
  disagreement was manufactured by the alignment.

Measured effect of all three fixes, plus a confidence floor below which a read is not
published: precision and recall **0.0% -> 29.6%**, character error rate **39.8% ->
26.9%**, reads asserted on crops no human can read **21 -> 0**. Four of the seven
government-feed crops from camera 7 are now read exactly right.

**The lesson worth carrying.** All three defects were invisible to code review -- each
looked reasonable, and two were accompanied by a comment explaining why they were
right. They were found by annotating real output by hand and scoring against it. A
pipeline that reports its own rates is not the same as a pipeline that is measured.

## Finding 16 - independent footage reproduces the resolution ceiling

**2026-09-01.** Looking for a second demonstration clip we could lawfully process, we
took *Moving vehicles in Link road, Cuttack, Odisha* from Wikimedia Commons (CC BY-SA
3.0, Subhashish Panigrahi) and ran the unmodified pipeline over 90 seconds of it.

| | |
|---|---:|
| Source | 1080x606, 25 fps, elevated wide shot of a junction |
| Frames decoded | 2,250 |
| Frames passing the motion gate | **4** (0.2%) |
| Plate boxes detected | **0** |
| Grammar-valid registrations | **0** |

Zero, from footage full of vehicles. Two things caused it, and both matter more than
the failure itself.

**The plates are a handful of pixels.** At 1080x606 from an elevated position, a car
occupies perhaps 40 px across and its plate maybe 8. There is nothing there to read.
This is Finding 12 reproduced on entirely independent footage, by a different camera,
in a different state -- which makes it a property of the *viewing geometry*, not of one
estate's encoder settings.

**The motion gate barely fired**, and that is the more interesting half. A static camera
watching distant traffic produces almost no inter-frame difference at the gate's
downscaled working size, so 99.8% of frames were discarded as "nothing moved" while
dozens of vehicles crossed the scene. The gate is behaving exactly as designed and is
exactly wrong for this geometry. It is tuned for subjects large enough in frame to be
worth recognising -- which is the same condition ANPR itself needs, so the two failure
modes coincide rather than compounding, but a deployment at this camera height would
need the gate reconsidered, not just a better recogniser.

**Why this is recorded rather than discarded.** It is the strongest independent support
for the scaling argument in the HLD. The claim that centralised processing costs ~98,500
CPU workers rests on needing full resolution, and the obvious objection is that we are
being precious about image quality. Here is third-party footage, a real Indian junction,
plainly full of traffic, that yields nothing at all. Camera placement and resolution
bound this problem far more tightly than model choice does.

The clip was therefore **not** shipped as a second demonstration feed: a demo that
displays zero reads demonstrates nothing to a judge. It is cited here as a measurement.

## Finding 17 - the estate moved, and RTSP became reachable

**2026-09-02.** The gateway is no longer `live.corp8.cloud`. A new one answers, with a
different address, a different access model and a very different catalogue.

| | Before | Now |
|---|---|---|
| Catalogue | `live.corp8.cloud/api/ingest`, open | `cctv.corp8.cloud/cameras.json`, behind a password |
| Catalogue fields | urls, codec, resolution, fps, bitrate, `live` | **`id`, `name`** |
| RTSP | 8554 unreachable (Cloudflare proxies 443/80) | `103.250.160.189:8554`, **open** |
| HLS | `/live/stream/{id}/index.m3u8` | `/{id}/index.m3u8`, authenticated, `PLAYLIST-TYPE:VOD` |

Three consequences, in order of how much they changed.

**RTSP works.** This is the finding that overturns an architectural decision. Findings 6
and 10 established that 8554 was unreachable and HLS was the only viable transport; ADR
0002 was written on that basis. The media plane is now on a bare IP a CDN is not in front
of, and the preflight decodes frames over forced TCP with a 0.12 s join. See the addendum
to ADR 0002.

**The catalogue now declares nothing.** Finding 1 recorded that 19 of 30 cameras declared
no codec or resolution, and concluded that capabilities must be probed rather than
trusted. The new catalogue declares none for *any* camera and carries no `live` flag at
all. The policy needed no revision -- only the numbers moved -- which is the strongest
evidence so far that "probe, never trust" was the right call rather than a defensive one.

**HLS is authenticated, and therefore not a usable ingest fallback.** The playlist sits
behind the login. A Python client with a session cookie can fetch it; FFmpeg, which is
handed a URL and nothing else, cannot. Both transports were tested with the media plane
down and both failed, but the failures are different in kind: RTSP was unavailable, HLS
is unavailable *to FFmpeg* whenever the session is not in the request. RTSP needs no
authentication and is the working ingest transport on this estate.

### Availability swings within the hour

| Time (2026-09-02) | Observation |
|---|---|
| ~05:00 | Preflight discovered 3 cameras delivering; 8/8 with every check exercised |
| ~05:35 | A full 30-camera ingest sweep: **0 producing frames** |
| ~06:00 | Cameras probed individually, paced, one at a time: **none answered**; port 8554 still open |

The 05:35 result had a cause we could fix and did: opening thirty captures back to back
is a load pattern this estate answers by refusing all of them, and the organiser's own
guide asks clients to pace their load. The 06:00 result did not -- that one is the estate.

### What the cameras actually show

The detector was run over one still from each of the 25 cameras that answered.

| | |
|---|---:|
| Plate boxes found, all cameras | **1** |
| Its size | **66 x 21 px** |
| Its read | `AAA1649`, character confidence 0.28 |

The crop is illegible to a human. Most feeds are night footage on a loop -- the burnt-in
clock reads `14-06-2026 05:04` against a real date of 2 September -- and only two cameras
show daylight. Finding 12 and Finding 16 reproduced a third time, on a different estate
with different cameras: **camera placement and resolution bound this problem far more
tightly than model choice does.**

## Finding 18 - tiling finds more boxes and fewer plates

**2026-09-02.** The obvious optimisation, measured, and rejected.

The detector is a 384x384 model. Handed a 1920x1080 frame it resizes the whole thing, so
a plate 66 px wide in the original reaches the network at about 13 px. Splitting the
frame into overlapping tiles and detecting on each keeps the plate nearer its native
size, which should find plates the whole-frame pass misses. It does. That turns out not
to be the same as reading more of them.

**On the live estate** (12 frames, cam01 and cam04): whole-frame found **0** plate boxes,
tiling found **5**. Every one was a false positive -- a wall, a kerb, headlight glare --
and the recogniser correctly returned an empty string for all five. The crops were
enlarged and looked at; none contains a number plate.

**On footage that genuinely has readable plates** (40 frames of the own-feed clip, the
same footage the 29.6% figure is scored against):

| | whole frame | tiled |
|---|---:|---:|
| Plate boxes | 12 | **57** |
| Grammar-valid reads | 8 | **8** |
| Distinct "valid" plates | 5 | **7** |

Five times the boxes and not one extra valid read. Worse, the distinct-plate count went
*up*, and that is a loss rather than a gain: the clip contains one vehicle,
`KA25AB1542`. Whole-frame found it. Tiling produced `KA25AB1116`, `KA25AB1561` and
`KA2SAB156` -- plausible-looking strings that satisfy Indian plate grammar and are
wrong -- **and lost the correct plate entirely.**

The mechanism is worth stating, because it generalises. Extra detections are not free.
Each false box is handed to a recogniser that will always return *something*, and the
grammar filter cannot tell a confident misread from a real registration. So an
optimisation that improves recall on the detector improves the supply of fabricated
registrations at the same time. On this estate that trade is entirely one-directional:
there are no plates to gain and only noise to add.

**Not shipped.** The tiling code exists only in this record. The whole-frame path stays.

This is the third measurement in three days pointing the same way, and it is the answer
to "are we doing something wrong with the feed?": no. The pipeline decodes the live grid
at full resolution, gates it, detects on it, and passes 8/8 of the organiser's own feed
contract. What is missing is not processing but pixels on the plate -- and the standard
trick for recovering small objects makes the output less truthful rather than more.

## Finding 19 - the government cameras were never placed on the map

**2026-09-02.** Model 1 is the one mandatory model, and its whole point is a GIS
registry. All thirty government cameras were sitting at `geom_source: unset`, with
`lat` and `lon` null, so none of them appeared on the map, none could anchor a journey
hop, and the coverage report counted every one of them as a gap.

Nothing needed building. `scripts/geocode_cameras.py` already resolves a camera's
free-text name against Nominatim with provenance tiers, and it had been run for the
previous estate -- the four `REPLAY-` cameras carry `geocoded` and `approximate`
positions from it. It had simply never been run against the estate that replaced it.

Running it: **18 geocoded, 6 district-centroid, 6 honestly unset** of thirty. Coverage
gaps fell from 34 to 28. The results were applied through `POST /cameras/bulk-import`
rather than SQL, because that is the Model 1 capability a judge is meant to see, and
using it here means it is exercised rather than merely demonstrated. It reported
29 updated, 0 created, 0 rejected.

Two things worth recording, because both are the kind of detail that decides whether a
map is trustworthy.

**The seed CSV was keyed on the old estate's references.** Its `camera_ref` column held
`1`..`30` while the live registry uses `cam01`..`cam30`, so importing it unchanged
would have created thirty new cameras rather than placing the existing ones. The rows
were re-keyed by matching the camera's name, which matched 29 of 30 exactly; the
estate had renamed `14 Delight` to `14 Delight RLVD`, and that camera resolves to
nothing anyway.

**One coordinate was wrong, and its confidence radius made it worse.** Every placement
was checked against the city its own label names. Twenty-two of twenty-four sat within
a few kilometres. `02 Janpath` sat **64 km** from Ahmedabad: Nominatim had answered the
query `Janpath, Ahmedabad, Gujarat, India` with a guest house on the Palanpur highway
whose name merely contains the word, and the geocoder accepted it as a confident hit at
a **300 m** radius. A wrong position with a tight radius is worse than no position,
because route reconstruction consumes that radius as its tolerance. It has been
downgraded to the Ahmedabad district centroid at 5,000 m, with the rejection recorded in
`resolved_by`.

The general weakness stays: the acceptance rule looks at the geocoder's rank, not at
whether the answer is anywhere near the place the label names. A distance check against
the named district would catch this class, and is not written yet.

One check in this session was also wrong in the other direction and is worth admitting:
`34 dhanori` was first flagged as 267 km out of place, because the reference city used
for the comparison was picked badly. It is 14 km from Navsari and correct.

## Finding 20 - three ways the plate ceiling was attacked, and what each returned

**2026-09-02.** After the estate was fully onboarded, the remaining gap was the one
that matters most: 3,292 frames across 26 live cameras and **zero** grammar-valid
registrations. Three separate attempts to move that number, each measured rather than
argued.

**Upscaling the crop before OCR: a no-op.** The detector can hand the recogniser a
crop 60 px wide, and `fast_plate_ocr` resizes internally, so a better interpolation
first might plausibly help. Run over 40 frames of the own-feed clip at 1x, 2x, 3x and
4x cubic upscale, the results are not merely similar, they are **identical**: 8
grammar-valid reads, 5 distinct plates, the same five strings, `KA25AB1542` present in
all four. The library normalises the input itself and nothing upstream of that
survives. Not shipped, because there is nothing to ship.

**Tiling: rejected earlier, see Finding 18.** More boxes, no more plates, and it lost
the correct registration.

**The one real plate this estate has produced is two-line.** `cam22` yielded a genuine
yellow commercial plate, enlarged and inspected: the lower row reads roughly `AZ 90 2`
and the upper row is an unrecoverable smear. Our recogniser reads a single line of up
to ten characters, so a two-row plate cannot form a complete registration even when
legible -- the read returns one row and the Indian grammar filter correctly rejects it.
This is worth recording as a genuine capability gap rather than a resolution problem,
but building two-line handling now would be building against a single illegible
example, which is the mistake this project keeps declining to make.

The conclusion after three attempts is unchanged and now better evidenced: what is
missing is pixels on the plate. `AZ9072` is the useful datum, because it separates the
two failure modes we keep having to explain. The detector is not merely firing on
signage; it found a real plate. The plate was not readable.

## Finding 21 - the thin catalogue is the estate's, not our parsing

**2026-09-02.** Before writing that the estate publishes no department attribution,
three sources were checked rather than one.

`GET /api/ingest` returns **1,373 bytes for thirty cameras**, and the union of keys
across all of them is exactly `["id", "name"]`. The estate's own control-room page
fetches a different path, `cameras.json`, which raised the possibility that the richer
document existed somewhere and we were reading the wrong endpoint: it returns the same
1,373 bytes and the same two keys. The dashboard HTML itself contains no occurrence of
`department`, `dept`, `owner`, `agency`, `GSRTC`, `Panchayat`, `Municipal`, `Health` or
`Police`.

So the integration guide's promise -- "every camera with its id, location, codec, live
status, stream properties, and all three URLs" -- is not met by anything the estate
serves, and the pre-submission checklist item "camera list and per-camera properties
are read from `/api/ingest`" cannot be satisfied by any participant. Raised with the
organisers in `docs/SUPPORT_QUERY.md`.

The consequence for us is stated rather than worked around: department attribution is
unavailable for 29 of 30 cameras, and inventing it would mean fabricating the single
field a department-scoped access-control model exists to protect. Per-camera codec,
resolution and frame rate *are* recoverable, because they can be measured from the
decoded stream, and now are.

## Finding 22 - the estate produced a Gujarat registration, on the fourth try at the ceiling

**2026-09-03.** Three preprocessing ideas had been measured and rejected; a fourth was
run, and then a live ingest returned something none of them could have.

**Crop preprocessing does not help. Measured, six ways, on the own-feed clip** (40
frames, scored on grammar-valid reads and on whether the one plate the clip contains
survives):

| variant | valid reads | distinct | `KA25AB1542` found |
|---|---:|---:|:--|
| **none** | **8** | **5** | **yes** |
| CLAHE | 3 | 3 | yes |
| histogram equalisation | 0 | 0 | no |
| sharpen | 5 | 5 | **no** |
| bilateral denoise | 8 | 6 | yes |
| CLAHE + sharpen | 1 | 1 | no |

Nothing beats leaving the crop alone. Histogram equalisation destroys the read
entirely and sharpening loses the correct plate. Denoise ties on count while producing
one *more* distinct plate from a clip containing one vehicle -- which is a loss, not a
tie, by the same argument that rejected tiling: an extra distinct plate here is an
extra fabricated registration.

So the pipeline is at its ceiling for this footage, and the ceiling is pixels on the
plate. That is now four measured attempts -- tiling (18), upscaling (20), preprocessing
(here) -- and four rejections.

**And then the estate gave us one.** A live ingest of four cameras on 3 September,
30 seconds each, returned from `cam22`: 148 frames, 9 plate regions, one fused record,
`GJ09BM3641` at confidence 0.757 -- **the first grammar-valid registration this
government estate has produced**. The crop was enlarged and read by eye: it is
unmistakably a real Gujarat plate. The digit after `GJ` is legible enough to be `8` or
`9` and we do not claim to have settled which, so the read is *plausible and possibly
one character wrong*, exactly as `GJ32AG0344` was on 2 September.

It flowed the whole way through without anything being tuned for it: persisted to the
deployed database, matched by the zone classifier against the `Near carriageway`
polygon on that camera, and raised as a `zone_intrusion` alert visible on the Alert
Desk. The README's "zero grammar-valid registrations from the government feed" has been
corrected to one.

The honest reading is not that the ceiling moved. 3,938 frames across 25 live cameras
have now yielded 18 plate regions and **one** registration. What changed is that the
claim "this estate cannot produce a readable plate" was too strong, and a single
counter-example is enough to require saying so.

## Finding 23 - `detection.vehicle_bbox` holds a plate box, not a vehicle box

**2026-10-09.** The Phase 2 brief scoped appearance-based identity on the premise that
"`Detection.vehicle_bbox` already exists -- the vehicle box is already being captured
alongside the plate box." It is not, and the column name is why the premise was
believed.

The chain, traced rather than assumed:

* `persistence.py:100` writes `PlateDetectionRecord.bbox` into `vehicle_bbox`.
* `anpr.py:580` sets that field from `track.bbox`, where `track` is a `PlateTrack`.
* `PlateTracker` associates **plate** boxes by IoU between plate boxes.
* The only detector in the project is `yolo-v9-t-384-license-plate-end2end`.
* `vehicle_counting.py`'s own docstring states it outright: "a detection is a *plate
  read*, not a vehicle."

So the platform had no vehicle box anywhere and no detector that could produce one. The
practical consequence is larger than a naming defect: the pipeline only ever learned a
vehicle existed when its plate was legible, which on this estate is one grammar-valid
read per ~3,900 frames (finding 22). Appearance matching had nothing to fingerprint.

Closing it needed a second detector, not a column rename. YOLOX-tiny (Apache-2.0,
Megvii) ships: 20 MB ONNX on the `onnxruntime` already present, 80 COCO classes of which
four are vehicles. `ssd_mobilenet_v1` and `tiny-yolov3` from the ONNX Model Zoo were
considered and rejected on small-object performance, which is the whole problem at a
median 27 px vehicle. Ultralytics remains excluded on licence grounds (ADR 0003).

Megvii's release is exported **without** `decode_in_inference`, so its single
`[1, 3549, 85]` output is grid-relative and log-scale -- 3549 = 52² + 26² + 13² anchors
over strides 8/16/32. Feeding it straight through puts every box near the origin, which
reads as a weak model rather than a missing step. The decode is ours and is unit-tested
against hand-built tensors.

**Measured on the own-feed clip:** 302 frames decoded, 52 analysed at 5 analytic fps, 74
vehicle detections, **11 sightings** emitted (one per pass, not per frame), all 11
fingerprinted. For comparison, the ANPR pipeline's yield on the government estate is one
registration per 3,938 frames.

Whether YOLOX-tiny finds a 27 px vehicle on the government feed is **not measured here**
and is not claimed.

## Finding 24 - a CIELAB histogram used the obvious way is worse than RGB would have been

**2026-10-09.** The appearance descriptor was specified as "colour histogram in CIELAB
(perceptually uniform, so 'same colour under different streetlight' is a small distance
-- RGB is not)". The reasoning is correct. The obvious implementation of it is not.

The first version binned all three channels into one 4×4×4 cube. On a synthetic pair it
measured **backwards**:

| pair | cosine distance |
|---|---:|
| same vehicle, dimmer light | **0.333** |
| plainly different vehicle | **0.119** |

Lightness occupies a third of a joint cube, so dimming a crop migrates most of its mass
into different bins and the distance explodes, while two unrelated vehicles that happen
to share a brightness profile land close together. The joint cube reintroduces exactly
the illumination sensitivity Lab was chosen to remove.

Splitting the axes and weighting them separately fixes it -- chroma (a, b) at 8×8 and
full weight, lightness at 4 bins and weight 0.30, edge orientation at 8 bins and weight
0.50:

| pair | cosine distance |
|---|---:|
| same vehicle, dimmer light | **0.035** |
| different vehicle (red) | 0.514 |
| different vehicle (blue) | 0.553 |

The lesson is not about Lab. It is that "use a perceptually uniform colour space" is a
property of the *space*, and collecting the benefit is a property of how the space is
*binned and weighted*. The test that catches this is the first one in
`test_appearance_descriptor.py` rather than an afterthought.

## Finding 25 - appearance separates vehicles, but far too weakly to assert identity

**2026-10-09.** The capability was measured in the same pass as it was built, using the
tracker's own association as ground truth: two views inside one track are the same
vehicle seen at different scales, poses and lighting as it crosses the frame; two views
in different tracks are different vehicles. No hand labelling and nothing simulated.

1,070 same-vehicle pairs against 24,581 different-vehicle pairs, from the own-feed clip:

| | cosine distance |
|---|---:|
| same vehicle, median | **0.0162** |
| same vehicle, p90 | 0.1123 |
| different vehicles, p10 | 0.0654 |
| different vehicles, median | **0.1911** |

There is real separation -- the medians differ by 12× -- and the tails overlap badly:

| gate | recall | false-link rate | precision |
|---:|---:|---:|---:|
| 0.05 | 0.704 | **0.055** | 0.356 |
| 0.10 | 0.861 | 0.213 | 0.149 |
| 0.20 | 0.980 | 0.526 | 0.075 |
| 0.39 | 0.993 | **0.834** | 0.049 |

**The gate the scorer's weighting originally admitted was 0.39.** It would have linked
five of every six pairs it proposed to the wrong vehicle. It was set by reasoning about
what the weights ought to mean, it was wrong by an order of magnitude, and only the
measurement found it. The shipped gate is **0.05**, chosen on the same principle as the
ANPR publish floor: a wrong answer sends an investigator somewhere else entirely, while
nothing at least prompts them to look again.

**Precision at the shipped gate is 0.356.** On this footage roughly two of every three
proposed links would be wrong. That number belongs in front of a jury unprompted, and it
is the entire reason an appearance match is rendered as `CANDIDATE` and never in the
style of a plate read.

**Quality gating does not help.** The brief expected it to; it was measured and it does
not. Raising the minimum crop quality from 0.0 to 0.6 moved the false-link rate at a
0.20 gate from 0.526 to **0.556** -- the wrong way, consistently, on both clips. Quality
is therefore kept as a *weight* on the composite score, where it demonstrably helps rank
a solid pair above a lucky noise match, and is **not** used as a filter.

Three caveats, all of which cut against flattering the figures:

* The footage is a BRTS bus depot -- a fleet in one livery -- so the different-vehicle
  set is adversarially similar. It is also a realistic policing case (a road of white
  hatchbacks), so it is not discounted.
* `demo_clip.mp4` is an excerpt of `commons_traffic_clip.mp4`. Running the measurement on
  both returned near-identical distributions, which is **not** independent corroboration
  and is not presented as such.
* The deployed search additionally requires a *different camera* and physical
  reachability under `speed.py`'s bound, neither of which this measurement models. The
  operational false-link rate should therefore be lower than 0.055. It has not been
  measured, so it is not claimed.

The honest summary: this is a search aid that narrows thousands of sightings to a handful
a human then adjudicates, with both crops shown side by side. It is not an identification.

## Finding 26 - a gap in the audit ledger's sequence is not a hole in the audit ledger

The backup/restore drill (`backend/scripts/backup_restore_test.py`) failed on its first
complete run, on one check out of eight:

```
[ok ] row counts match: every table matches
[ERR] audit chain intact: 1696 entries, 0 broken links, 3 sequence gaps
[ok ] PostGIS geography preserved: 267 placed cameras, digests match
```

Zero broken hash links and three gaps in `seq` is a strange pair of numbers. A restore
that lost entries would break the chain, because each entry's `prev_hash` names the one
before it. Querying the **source** database returned the same three gaps, in the same
places:

```
seq   1 ->   3   (1 value never committed)
seq  59 ->  61   (1 value never committed)
seq  66 -> 127   (60 values never committed)
```

The dump was faithful; the check was wrong. `seq` is drawn from a Postgres sequence, and
a sequence is deliberately non-transactional -- it hands out a number before the
transaction commits and does not reclaim it on rollback, because reclaiming would make
every `nextval` wait on every other writer. So a rolled-back write permanently consumes
its number. The 60-wide gap is recognisably the ingest run recorded in this document that
rolled back when RLS refused its writes: the attempt drew 60 sequence values and
committed none of them.

The drill now asserts that the restored gaps **match the source's**, which is what
faithfulness actually means, and keeps asserting zero broken links, which is what makes
the ledger evidence. It reports the gap count as an observation about the source rather
than a failure.

Worth stating because the opposite mistake is the expensive one. A reviewer who reads
"gap in the audit sequence" as "entries were deleted" would be right to be alarmed about
an append-only ledger -- and would be wrong here, for a reason that has to be written
down rather than argued in the room. The property the ledger guarantees is that no
committed entry can be altered or removed without breaking the chain. It does not
guarantee that every integer is used, and it never did.

Drill result: `docs/evidence/backup_restore_drill.json`. Eight checks, all passing,
0.3 MB dumped in 2.2 s and restored in 2.8 s, with PostGIS geography digests matching
and `appearance_vector` restored as `vector(76)`.

## Finding 27 - the accessibility pass found three things, and two were invisible

§8 asks for a WCAG AA contrast check, a responsive audit, and verification that Indic
camera names render. Each was done as a measurement rather than a review, and two of
the three failed in ways nobody would have noticed by looking.

**Contrast.** Computed from the theme tokens rather than judged by eye, because
distinguishing 4.3:1 from 4.5:1 is not something a person can do. The dark theme passed
outright. The light theme had two failures:

```
muted          on ink-700   4.31:1  (needs 4.5)
grade-unknown  on ink-900   2.43:1  (needs 3)
grade-unknown  on ink-800   2.56:1  (needs 3)
grade-unknown  on ink-700   2.32:1  (needs 3)
```

`grade-unknown` is the badge worn by a camera the estate has not yet graded, which at
the time of measurement was **477 of 477 cameras**. The least legible colour on the
palette was also the most frequently rendered one. Both tokens were darkened and the
check is now `npm run check:contrast`, which exits non-zero, so this cannot regress
quietly the way it arrived.

**Responsive.** Eight screens at 375, 390, 768 and 1024 px in both themes, asserting
that the document does not scroll horizontally and that no element escapes the
viewport. No findings: 64 page-width-theme combinations, zero overflow. This one
passed, and it passed because the earlier work on the journey and GIS layouts had
already fixed the two cases that did overflow.

**Indic rendering.** This is the one worth writing down.

First, the estate's own data: of 491 cameras, **none** carries a Gujarati or Devanagari
character. Every camera name and location string is ASCII. The only non-ASCII codepoint
anywhere in the registry is U+00B7, a middle dot this project introduced in its own
replay labels. So "verify with real strings" has no real strings to verify against, and
what follows is a readiness check, not a verification against production data. Saying
otherwise would be the easy and dishonest version.

Second, the bug. The Google Fonts link for Noto Sans Gujarati and Noto Sans Devanagari
was present, and both faces were in the `font-family` stacks, in `index.css` and in the
Tailwind `sans` role. Both correct, and both insufficient. Measured in the browser on a
fully loaded page:

```
Noto Sans Gujarati     false
Noto Sans Devanagari   false
IBM Plex Sans          true
```

A browser does not download a webfont until something on screen needs a glyph from it,
and nothing on the console is in Gujarati. The Gujarati sample rendered anyway -- via a
Windows system font that happened to be installed. On a workstation without one, which
is an entirely ordinary state for a kiosk or a locked-down police build, the stack falls
through to `system-ui` and a camera named in Gujarati renders as tofu. The failure would
appear only on the machines nobody develops on.

The faces are now requested by name at startup. `document.fonts.load()` returns one face
each and the check flips to true with no Indic text anywhere on screen. Not awaited:
Indic text is not on the first screen, and blocking first render on a font request would
trade a rare failure for a universal delay.

The general shape of this, which is why all three were measured rather than reviewed: a
contrast ratio, a horizontal overflow and a missing webfont are all invisible on the
machine of the person who built them. Two of the three were wrong.

## Finding 28 - a new access code does not return your watch time

Measured 2026-10-09/10, on a freshly issued gateway access code.

The gateway has two planes and they fail independently. With the new code configured,
the **control plane answers normally**:

```
GATEWAY REACHABLE: 30 cameras in the catalogue
/health/gateway -> reachable=true, consecutive_failures=0, last_error=null
```

The **media plane does not**. Requesting a camera's HLS manifest returns the gateway's
own sign-in page rather than a playlist, and performing the session login it asks for
returns:

```
gateway refused the sign-in: watch time limit reached
                             - please wait for your cooldown, then watch again
```

So the watch-time quota is bound to the **account**, not to the access code. Rotating
the code re-authorises catalogue access and does nothing at all for watch time; the
cooldown still has to elapse. This is worth stating plainly because the opposite belief
is the natural one -- a new credential usually means a clean slate -- and acting on it
burns the morning before a demonstration.

Two practical consequences, both already how the platform behaves:

The health poller calls `fetch_catalogue`, not a stream. It therefore does **not**
consume watch time, and a long-running deployment polling once a minute is not what
exhausts the quota. What exhausts it is watching. The 21,403 checks recorded on the
deployed instance had not spent a second of it.

Everything except live video keeps working through an exhausted quota, because every
other screen reads from the database rather than the gateway. Journey, Coverage, Blind
spots, the Dashboard, the Alert Desk and the audit ledger are all unaffected. The
demonstration runbook already leads with that, and this finding is why that ordering is
not merely defensive.

One correction to an earlier reading of this, recorded because the mistake is easy to
repeat: a direct request to the configured media host IP on port 8889 fails with an SSL
error, which looks like a gateway fault and is not one. `hls_url()` builds its URL from
`gateway_host` -- the hostname -- precisely so TLS can verify. The IP is used for RTSP
and WHEP, where there is no certificate to check. Probing the IP over HTTPS tests
nothing but the prober.

## Finding 29 - the appearance search returned nothing, and the matcher was fine

The Phase 2 headline capability returned an empty list on every plate in the
demonstration database. Nothing in the matcher was wrong. Its input never arrived.

The chain, in the order it was unpicked:

**1. The two halves sat in different weeks.** `seed_demo.py` back-dates its plate reads
to `now - 7h` and offsets each replay camera by the real travel time between them --
0, 100, 300 and 340 minutes, which is what makes the seeded route physically plausible
at roughly 60, 65 and 38 km/h. `ingest_sightings.py` had no equivalent and stamped every
camera at wall-clock now. The same clip replayed across four cameras hundreds of
kilometres apart therefore produced observations seconds apart, which `speed.py`
correctly rejects as impossible. `--offset-min` and `--base-hours-ago` fixed that.

**2. Each script computed its own anchor.** With offsets available, running the two in
sequence still failed: each resolves `now - 7h` when *it* starts, so the timelines ended
up as far apart as the gap between the two runs. `--align-to-reads` anchors the sighting
timeline to the plate reads already recorded for the first camera, so the operator does
not have to do that arithmetic and cannot get it wrong.

**3. The linking window was anchored to the clock.** This was the real defect, and the
only one that would have reached production. `ingest_sightings.py` linked plate reads to
sightings over `[run start - 1h, now + 1h]`. That is correct only when footage is being
ingested as live. Any back-dated ingest -- which is exactly what the two flags above
exist to produce -- writes sightings hours in the past, and every one of them fell
outside the window. The linker reported zero links **without examining a single row**:

```
before:  REPLAY-01: considered=0  linked=0
after :  REPLAY-01: considered=5  linked=2  no_container=3
```

"Considered 0" and "considered 5, linked 2" are completely different findings, and the
first was being rendered as a quiet, successful-looking zero. The window now derives
from the sightings the run actually wrote, which covers the live case and the
back-dated case alike and cannot drift.

With all three fixed, the search works end to end:

```
KA25A6156: seeds=3  sightings searched=417  candidates=4  (192 ms)
   REPLAY-03  probable  score 0.97  d 0.0000  bus  60 km/h
   REPLAY-02  probable  score 0.96  d 0.0000  bus  64 km/h
```

**The 0.0000 is the replay harness, not the method.** All four REPLAY cameras play the
same recorded clip, so two of them can hold byte-identical frames and the cosine
distance between their descriptors is exactly zero. On footage from genuinely different
cameras it never is -- finding 25 measured 0.035 for the same vehicle and 0.514 for
different ones. The API response now says this in its own caveat whenever a result
involves a REPLAY camera, because a reviewer who sees a perfect score and only later
learns the inputs were the same file has been misled regardless of intent.

The general lesson, which is the same one as finding 23 and finding 27: a zero that
means "nothing matched" and a zero that means "nothing was looked at" are different
facts, and code that renders them identically will hide the second one for as long as
nobody runs the path end to end.

## Finding 30 - a thirty-camera sweep is the whole watch-time budget

Measured 2026-10-10, on a newly issued access code with the quota freshly recovered.

An attempt to record vehicle sightings from the government feeds ran
`ingest_sightings.py --gateway --seconds 20`, which opens each of the thirty catalogue
cameras in turn for twenty seconds. The result:

```
30 cameras, 0 frames decoded, 0 vehicles, 0 sightings
```

and immediately afterwards the catalogue itself went from reachable to:

```
GatewayQuotaExhausted: watch time limit reached - please wait for your cooldown
```

So the sweep consumed the account's entire watch-time allowance and returned nothing
for it. Two things follow, and both matter more than the failed ingest:

**The budget is smaller than one pass over the estate.** Thirty cameras at twenty
seconds is ten minutes of requested video. That is not a stress test; it is the
smallest useful sample of the whole estate, and it does not fit. Any plan that assumes
the government feed can be swept on the day is wrong. The demonstration runbook's
existing instruction -- lead with the screens that read from the database -- is not
defensive framing, it is the only thing that survives.

**Exhausting it also takes the catalogue down.** The control plane and the media plane
authenticate against the same account, so spending the watch time removes camera
metadata as well as video. `/health/gateway` reports `reachable: false` for a reason
that has nothing to do with the catalogue endpoint. Finding 28 recorded that a new
access code restores the first and not the second; this is the same coupling seen from
the other side.

The honest accounting: this quota was spent by us, deliberately, to find out whether
the feeds would yield sightings. They yielded none before the budget ran out, so the
question is still open. It is recorded here rather than quietly retried.

## Finding 31 - thirty cameras reported ok, and not one of them produced a frame

The sweep above surfaced a reporting defect worth more than the sweep.

Every camera came back `ok=True, error=None, frames_decoded=0`, and the run summary
counted **thirty successes**. Nothing had raised an exception, so nothing was recorded
as a failure -- and nothing had worked either.

```
[ok ] cam11  frames=0  vehicles=0  sightings=0
[ok ] cam12  frames=0  vehicles=0  sightings=0
   ... thirty times
```

A total feed outage presented as a clean run. That is the most expensive way for this
particular failure to present, because the next person reads the summary line and
concludes the feeds are fine.

"Did not raise" and "produced data" are different claims, and only the second is what
a caller means by `ok`. A source that opens without error and delivers no decodable
frame is now reported as failed, with that sentence as its error.

This is the third instance of the same shape in this document -- finding 23's zero
detections, finding 29's zero links, and now thirty zero-frame successes. The pattern
is always a zero that could mean "nothing was there" or "nothing was looked at", and
code that renders the two identically. It is worth watching for deliberately.

## Finding 32 - a size filter selected against the evidence it was filtering for

The hosted instance cannot run vehicle detection, so the sightings it serves come from a
committed fixture: real detector output, computed once on a machine with a processor.
Forty sightings, ten per replay camera, each with the crop the detector cut.

Loaded into the deployment, it produced **zero plate links**. The appearance search had
forty vehicles to search and not one vehicle with a plate to contrast them against --
which is the entire argument for the feature. A plate-linked sighting is the control: it
is what lets the screen say *this* one a plate-based system would have found, and these
forty-odd it would not.

Two independent causes, and the second is the interesting one.

**The window.** The fixture took the earliest ten sightings per camera, by stream
position. That put all forty inside the first three seconds of the clip:

```
REPLAY-01  pts 1802 .. 1802      plate reads on this camera:
REPLAY-02  pts 1802 .. 2202        5404, 8808, 10009, 10209, 16815 ms
```

Every plate read on that footage falls after the fifth second. The two sets could not
overlap, so the linker's `+/-400 ms` stream-position condition was never satisfiable.

**The size ceiling.** Crops over 60 KB were dropped, to keep the committed fixture small.
That ceiling is not a neutral sample. A crop is large when the vehicle filled the frame,
and a vehicle that fills the frame is exactly the one whose plate was legible. Every
plate-linked sighting on this footage was over the ceiling -- 132 KB and 343 KB -- so even
after fixing the window, the filter removed precisely the rows that made the point.

The 343 KB crop is 1478x912. It does not reach 60 KB at any JPEG quality worth showing.

Both are fixed in `export_sighting_fixture.py`: linked sightings are selected first, and a
crop over the ceiling is re-encoded at a lower quality rather than discarded -- at the
same pixel dimensions, so it still shows the region it showed, at the size it showed it.
The ceiling moved to 150 KB; the fixture is 1.9 MB. The link is not exported and is
recomputed in the deployment by the same linker, from its own plate reads.

| | before | after |
|---|---|---|
| sightings | 40 | 40 |
| stream positions covered | 1802-3003 ms | 1801-26691 ms |
| plate-linked, once loaded | 0 | 2 per camera where that camera has reads |

The shape worth naming: **a filter applied for an unrelated reason (bytes) correlated with
the property being measured (plate legibility), and silently removed the signal.** The
count stayed at forty throughout, so nothing looked wrong.

## Finding 33 - the accuracy denominator counted one image four times

29.6% precision and recall, 26.9% character error rate, over 27 annotated crops. Hashing
those crops says something the annotation file cannot: there are **12 distinct images**.
The four `REPLAY-*` cameras replay one recording, `annotate` enumerates crops per camera,
and five crops are therefore annotated four times each, byte for byte identical.

```
51f625717bed x4  REPLAY-01,02,03,04   read KA25AB1542  truth KA25AB1542
24ddca38a3fa x4  REPLAY-01,02,03,04   read 4425A1111   truth KA25AB1116
0d4483cca5d3 x4  REPLAY-01,02,03,04   read K4ZA11111   truth KA25AB1116
660affef3408 x4  REPLAY-01,02,03,04   read KLZ541111   truth KA25AB1116
58f9a9f747c6 x4  REPLAY-01,02,03,04   read KA25A6156   truth KA25AB1561
dbebad372d34 x1  cam07                read GJ32AA3900  truth GJ32AA3900
   ... six more, one each
```

So the headline is weighted by how many cameras happened to be pointed at the same
recording, which is a property of the demonstration rig. Counted once each, the *same
reads* give:

| | per annotated crop | per distinct image |
|---|---|---|
| crops | 27 | 12 |
| correct | 8 | 5 |
| precision / recall | 29.6% | **41.7%** |
| character error rate | 26.9% | **20.3%** |

`ground_truth.py score` now prints both and neither is presented as a correction of the
other. 29.6% stays the figure every document quotes: it is the lower number, and it is
the one a reader reproduces by running `score` against the committed CSV without knowing
anything about replay.

Worth naming because the duplication *flattered* the headline here as easily as it could
have depressed it -- the crop that reads correctly is one of the five repeated ones, so
the duplicates contributed 4 of the 8 correct reads. Either direction, the number was
measuring the rig.

## Finding 34 - a true pair at 3.03 diagonals, against a bound of 2.5

Three of the four worst reads on the own-feed clip are the same vehicle: `4425A1111`,
`K4ZA11111` and `KLZ541111`, annotated `KA25AB1116` in all three cases, at 8808 ms,
10009 ms and 10209 ms. Three records for one plate means multi-frame fusion did not
happen, and finding 23 already established that single-frame reads are where this
recogniser is worst.

The tracker rejects a pair whose box centre moves more than `max_travel_diagonals`
between analytic frames. Measured on the real boxes:

```
10009 ms  (336, 955, 424, 1016)  centre (380, 986)  diagonal 107 px
10209 ms  ( 14, 987, 101, 1051)  centre ( 58, 1019)
travel 324 px / 107 px  =  3.03 diagonals        bound was 2.5
```

Half a diagonal outside, at 200 ms apart. The pair at 8808 ms is 1201 ms later than its
predecessor and is held out by the separate 600 ms gap bound, which is a different and
defensible refusal.

The bound is now exposed as a parameter (`--max-travel-diagonals`) and swept over the
clip's 41 plate boxes:

| max_travel | fused records | grammar-valid | distinct valid plates |
|---|---|---|---|
| 2.5 (was) | 5 | 2 | 2 |
| 3.0 | 4 | 2 | 2 |
| **3.5 (now)** | **3** | **3** | **3** |
| 4.5 | 3 | 3 | 3 |
| 6.0 | 3 | 3 | 3 |

At 2.5 the vehicle produced `4425A1111` (6 frames), `K4ZA11111` (1 frame) and
`KLZ541111` (1 frame), all unparseable. At 3.5 it produces one 8-frame record reading
`KA25AI111` with two grammar corrections. That is still **not** the right plate --
`KA25AB1116` -- but it is a near-miss the grammar accepts instead of three strings it
rejects, and `KA25AB1542` is recovered as a clean 4-frame read at the same time.

3.5 is the bottom of the plateau, chosen for being the least permissive value that gets
the benefit. **6.0 not over-merging on this clip is not evidence that 6.0 is safe** -- it
is evidence that this clip does not contain the case that would catch it, which is a
different statement. The two geometries are pinned as tests so a future tightening has to
argue with the measurement.

### What has not been re-measured

**The 29.6% and 41.7% figures above are from the run with the 2.5 bound and are not
restated under 3.5.** Changing fusion changes which crops exist and what each one reads,
so the committed annotation -- which is keyed by crop filename -- no longer lines up.
Re-measuring honestly means running the pipeline again, writing new crops, and a human
annotating them by eye, which is the one step in this repository that cannot be
automated. Until that is done the accuracy claim stands at 29.6%, unchanged, and the
sweep is reported as what it is: three unparseable records became one near-miss and one
more plate became readable, on one clip.

## Finding 35 - a correct platform showing zeroes, because the seed is dated

A full chronological walkthrough of the hosted console, 22 screens, asserting the
*content* of each one rather than that it drew. Everything structural passed: coverage,
the registry, the dashboard's panels, the alert desk, the control room, onboarding, the
scale planner, health, blind spots, watchlist, zones, the audit ledger, the own-feed demo,
the command palette, the keyboard reference and the dark theme.

Three screens had nothing in them:

```
/find      0 vehicles, 0 evidence crops
/journey   Trace vehicle -> no route, no tiers, no export, no 3D control
/dashboard VEHICLES SEEN 0 · PLATE READS 0 · DISTINCT PLATES 0 · no class mix
```

One cause for all three. `seed_demo.py` places the replay timeline at `now() - 7h` **at
the moment it runs**, and the hosted database was seeded on 2026-09-03. Six weeks later
the demonstration data sits 41 days behind every default window in the console: the
dashboard opens on 24 hours, "Find a vehicle" on two days, and Journey traces inside a
bounded window.

Every row is present and every query is correct. The console reports zero, and a zero
from a correct query over an empty window is indistinguishable, on screen, from a
platform that does not work. This is the fourth instance in this document of a zero that
cannot say which of two things happened -- findings 23, 29 and 31 are the others -- and
the first where the ambiguity is in front of the judge rather than in a log.

`rebase_demo_timeline.py` applies one offset to the rows the seed itself placed: the four
`REPLAY-*` cameras' detections, their sightings and their alerts. It does **not** touch
the government-feed detections, whose timestamps are records of when a camera saw
something, and it does not touch the audit ledger. Re-running the seed is the intended
fix; this exists for the case where the container cannot be redeployed in time.

The general lesson, which is a deployment lesson rather than a code one: **demonstration
data placed relative to its seeding run has a shelf life, and nothing in the system
announces when it expires.** The dashboard should say "no data in this window" where it
currently says 0, and the window should widen to find the data rather than report its
absence at the default.

## Finding 36 - Judge Mode filled the form and stopped, and the first fix was tested wrong

A walkthrough against a database with data, following the brief's order and asserting the
content of each screen. The capabilities verified end to end, all on one trace:

```
Find a vehicle  48 vehicles, 1 with a readable registration, 48 crops rendering
Journey         4 hops · 413.4 km · 340.0 min, tiers on screen, signed export, 4 crops
3D              time-as-altitude opens, 2 canvases, survives rotation and pitch,
                scrubs to halfway, returns to the map with the route intact
Dashboard       402 vehicles seen (100.5/hour, peak 204) against 20 plate reads
Coverage        all four analytics modes answer
Onboarding      the probe reports stage by stage, including the stages it never reached
```

Zero requests returned 400 or above across the whole run.

**Judge Mode is two clicks, not one.** It signs in, derives the registration and the
window from the database, fills the form and navigates -- and leaves the trace unrun. The
reviewer lands on a populated form beside a panel reading "Enter a registration number".
`JourneyPage` reads every parameter into state and nothing calls the mutation.

Two things were done about it, and only one of them works.

**The registration it chooses (fixed).** `plate_normalised` holds unparseable recogniser
output beside real registrations, and `4425A1111` and `KA25AB1542` were each read at four
placed cameras over the same span -- every term the ordering had was equal, and the tie
broke toward the string that is not a registration. The screen built to demonstrate the
three-tier evidence model opened on a read that model rejects. Grammar-validity is now the
first ordering term, pinned by a test, and the live run confirms a valid plate is chosen.

**Running the trace (fixed, after a wrong diagnosis).** An effect that calls the mutation
once when the URL carries a plate and a purpose. Against the dev server it fired, the
server answered **200 in 1.5 seconds with four hops**, and the button sat on "Searching…"
indefinitely. I could not find the cause, decided a demonstration path that hangs is worse
than one needing a second click, and reverted it.

That was the wrong call, for a reason that should have been obvious. **React's StrictMode
double-mount is a development behaviour.** It mounts, unmounts and remounts; a mutation
started during the first of those is orphaned when its observer unmounts, and the
remounted observer inherits a pending state nothing will ever settle. The built bundle
does not do this.

Deferring the call to a zero-delay timeout fixes it in both: the timeout fires after the
remount has finished, so there is one observer and it is the live one, and the cleanup
cancels the discarded mount's timer. Verified against the **built** bundle served
same-origin with the API behind it, which is the shape Netlify and the backend have:

```
ok  a grammar-valid registration was chosen — KA25A6156
ok  the trace ran without a second click — 4 hops · 413.4 km · 340.0 min
ok  exactly one journey request was made — 1: [200]
ok  three-tier legend · signed export · 3D control all present
ok  editing the form does not re-fire the search — 0 extra
```

Then confirmed on the deployed console itself, which Netlify had already rebuilt from the
push: signing in with Judge Mode ticked draws the route with no second click --
`4 hops · 413.4 km · 340.0 min`, one journey request, 200, with the tier legend, the signed
export and the 3D control. The frontend half of this is live; the backend deploy that
re-anchors the timeline is separate and still pending.

The lesson is finding 38's, again and in a new costume: **a symptom observed under a
development-only behaviour is not evidence about the shipped artefact.** First it was an
unauthenticated 404 read as a missing file; then a dev server pointed at a dead port;
now StrictMode's double-mount read as a hung mutation. Three times in one session the
harness was the defect, and each time the way out was to test the thing that actually
ships.

Worth recording as a method note: the first three attempts to verify this reported
failures that were all the harness's. The walkthrough pressed "Trace vehicle" with no
registration and no purpose and read the form's refusal as a broken journey engine -- the
form was right, and the purpose reaching the audit ledger before any result is the point.
Then the browser was pointed at a dev server proxying to a port where nothing was
listening, and at a module the dev server had not reloaded. **Three of the four "product
defects" found by automation were defects in the automation**, and the only way that was
established was checking each against the API directly.

## Finding 37 - the re-ID comparison the brief asks for cannot be run on stored data

§3.2 asks for a small MIT/Apache ONNX re-identification or general embedding model to be
evaluated beside the handcrafted descriptor, and says a measured negative result is a
legitimate outcome. Finding 25 measured the descriptor and no model was ever put next to
it, so the brief's question had one answer and not the comparison.

Two Apache-2.0 graphs from the ONNX Model Zoo were fetched -- `mobilenetv2-12` (13.9 MB)
and `squeezenet1.1-7` (5.0 MB) -- and `compare_appearance_models.py` reproduces finding
25's method exactly: same-vehicle pairs from the tracker's own association, cosine
distance, and the recall / false-link / precision table at the same gates, so the rows sit
beside the existing ones.

It returns nothing, for a reason worth recording:

```
220 crops over 220 tracks
shipped: 76-dimension CIELAB + edge descriptor: not enough tracks to form both kinds of pair
```

`vehicle_sighting` holds **one row per fused pass**, not one per frame -- 489 sightings
over 489 distinct `track_id`s, confirmed by query. Finding 25's 1,070 same-vehicle pairs
came from per-frame crops during ingest, and those crops are not retained; only the one
crop chosen per pass survives. So the stored data contains no two views of one vehicle,
and the pair that the whole method rests on cannot be formed from it.

The replay cameras are not a way around this. They replay one recording, so a crop at the
same stream position on two cameras is the *same image*, byte for byte -- distance zero
for any descriptor, and no appearance variation at all.

What would unblock it: `ingest_sightings.py` keeping every frame's crop with its track id
behind a flag, and a detection pass over the own-feed clip. That is a code change plus a
run, not a measurement, so **this brief item is open, not answered.** The harness and the
models are committed so the measurement is one ingest away rather than a day's work.

## Finding 38 - a 404 that was the security control working

The evidence-image route was reported here, in a commit message and in the summary
document, as broken on the hosted instance:

```
$ curl -o /dev/null -w "%{http_code}" https://<api>/media/crops/sighting_01b55fdc5c9f.jpg
404
```

That 404 is correct. `/media/crops/{name}` takes `exp` and `sig` and serves nothing
without them: a crop is reachable only through a signed, expiring URL bound to one
filename. The signature exists because crop filenames are structured and therefore
guessable, and these are photographs of vehicles and their registrations assembled into
an investigative record. The console can still put them in an `img` tag, which a bearer
token cannot do, because the authorisation is in the URL.

So the probe was asking the question "is this file public?" and reading the correct answer
-- no -- as "this file is missing". Four turns of work were spent on a deployment that had
already landed, and the summary document carried the wrong claim until it was withdrawn.

Two things to take from it. **A request made without the credential the endpoint requires
is not a test of whether the endpoint works**, and the way to tell the difference is to
check something the deployment would only answer if the new code were live -- which in the
end is what settled it: `/demo/best-trace` returned a grammar-valid registration, which
only the new ordering does. And an unauthenticated 404 rather than a 401 is itself a
deliberate choice here: a 401 confirms the filename exists.

## Finding 39 - the hosted instance, verified in the order a judge meets it

**2026-10-11.** Twenty-two screens against the deployed console, asserting content and
counting the evidence images that actually decoded -- an image that 404s still occupies an
`img` tag, so counting tags proves nothing.

**Seventeen of twenty-two held.** The scored path works on the hosted instance today:

```
Judge Mode    chose KA25A6156, a grammar-valid registration
Journey       4 hops · 413.4 km · 340.0 min
              three-tier legend, signed PDF export, 4 evidence crops decoded
3D            time-as-altitude opens, WebGL draws, survives rotation and pitch,
              scrubs to halfway, returns to the map with the route intact
Coverage · GIS · Alert Desk · Control Room · Onboarding · Scale Planner ·
Health · Blind spots · Watchlist · Zones · Audit ledger · Own-feed demo   all answer
```

Judge Mode works there precisely because it derives its window from the chosen
registration's own reads rather than from a default -- the same fix finding 35's stale
timeline would otherwise have defeated.

The five that failed are one cause, already recorded as finding 35: the dashboard reports
0 vehicles seen and 0 plate reads, and Find a vehicle offers 0 vehicles, because the seed
is 41 days behind the windows those screens open on. The rebase now runs on every
container start, so a deploy clears all five.

**One failing request in the whole run**: `502 /media/gateway/cam01__index.m3u8`, four
times. That is the organiser's gateway, and the Control Room renders the failure properly
-- "Live feed unavailable · networkError: manifestLoadError · Camera cam01 · last known
status ACTIVE", with "The registry entry, coordinates and recorded detections for this
camera remain available. Only live playback is affected."

A freshly issued access code was tried and the gateway answered **"watch time limit
reached -- please wait for your cooldown"**. That is finding 28 repeating: a new code does
not return watch time. The control plane and the media plane fail independently, every
other screen reads from the database, and the demonstration runbook already leads with
those.

## Finding 40 - a general ImageNet backbone beats the handcrafted descriptor on precision

**2026-10-11.** §3.2 asks for an MIT/Apache ONNX re-identification or general embedding
model to be evaluated beside the handcrafted descriptor and the comparison recorded.
Finding 37 reported this as unanswerable from stored data, because `vehicle_sighting`
keeps one row per fused pass and so holds no two views of one vehicle.

That was a limit of the *stored data*, not of the question. `compare_appearance_models.py`
now runs the shipped detector and the shipped tracker over footage itself and keeps every
frame's crop with the track that owns it -- the ingest pipeline with its retention
changed and nothing else. Over 1,501 frames of the own-feed clip: 29 tracks, 25 of them
more than one frame, **1,471 same-vehicle pairs against 21,749 different-vehicle pairs**.
Comparable in size to finding 25's 1,070 / 24,581, and the shipped descriptor's numbers
land in the same place, which is the harness corroborating itself.

Two Apache-2.0 graphs from the ONNX Model Zoo, penultimate activations, L2-normalised,
cosine distance -- the same metric the descriptor is judged under, so nothing is flattered
by a different measure.

| | same-vehicle median | different median | best operating point |
|---|---:|---:|---|
| **shipped** 76-d CIELAB + edge | 0.0618 | 0.1995 | gate 0.02 → recall 0.311, false-link **0.007**, precision **0.737** |
| **mobilenetv2** 1000-d | 0.2465 | 0.4420 | gate 0.20 → recall **0.415**, false-link **0.014**, precision **0.672** |
| **squeezenet1.1** 1000-d | 0.0225 | 0.0497 | nothing usable |

**MobileNetV2 is the better descriptor on this footage**, and not marginally. At its own
best gate it reaches recall 0.415 at precision 0.672; the shipped descriptor needs gate
0.05 to reach a comparable recall of 0.455 and gets precision 0.357 there. Roughly double
the precision at the same recall. At gate 0.10 MobileNetV2 is at **precision 0.983 with a
false-link rate of 0.000** over 21,749 negative pairs, which no setting of the handcrafted
descriptor approaches.

SqueezeNet is the measured negative the brief allows for. Its distances collapse into a
band 0.02 wide: p10 of the different-vehicle set (0.0274) sits *below* the p90 of the
same-vehicle set, so every gate either rejects nearly everything or links nearly
everything. It is recorded because "we tried two and one was useless" is the honest shape
of a model comparison.

### Why the handcrafted descriptor still ships

Not because it is better. Three reasons, in order:

1. **Every stored vector is a 76-dimension one.** The pgvector column is typed to it and
   the HNSW index is built over it. Changing the descriptor means a migration, a
   re-ingest of every sighting and a rebuilt index -- not a configuration change.
2. **Gates and weights are tuned to its distance scale.** MobileNetV2's useful gate is
   0.20 where the shipped one's is 0.02; the composite scorer's weights assume the
   latter. Swapping the vector without re-tuning would be worse than either.
3. **One clip is one clip.** This is a bus depot. The finding is strong enough to act on
   and not strong enough to act on *untested*, four days before a demonstration.

So the recommendation is recorded rather than executed: **MobileNetV2 features, gate
re-swept, is the next change to the appearance search**, and the measurement above is the
evidence for it. The handcrafted descriptor remains interpretable and cheap, which is why
it was never a bad choice -- only, now, a measurably beaten one.

## Finding 41 - an UPDATE cannot move a row between hypertable chunks

The timeline rebase ran on the deployment and the timeline did not move. It was not
skipped and it did not silently no-op: it raised, and the entrypoint caught it into a log
line nobody reads.

```
psycopg.errors.CheckViolation: new row for relation "_hyper_1_1_chunk"
violates check constraint "constraint_1"
[SQL: UPDATE detection SET observed_at_utc = observed_at_utc + %(off)s ...]
```

`observed_at_utc` is the hypertable's partitioning column, and every chunk carries a CHECK
constraint bounding it to that chunk's range. An UPDATE that moves a row out of its chunk
does not relocate the row -- it fails the constraint. An offset of six weeks moves **every**
row out of its chunk by definition, so the UPDATE this was written as could never have
worked for the case it exists to serve.

Copy to a temporary table, shift there, delete, insert back. A temp table has no chunk
constraint, and the insert lets the hypertable route each row to the chunk it now belongs
in. One transaction, so a failure leaves the timeline where it was rather than half-moved.

Run against the deployment: 20 detections, 40 sightings and 16 alerts moved 42 days
forward, and the console went from zeroes to **40 vehicles seen, 20 plate reads, 20
distinct plates, 17 open alerts, Cars 85% / Motorcycles 15%**, with all four replay
cameras in "busiest cameras" and 40 evidence crops decoding on Find a vehicle.

The shape to carry forward: **a failure path that logs and continues is a failure path
nobody sees.** The entrypoint deliberately degrades rather than refusing to boot, which is
right for a seed -- and it meant this bug survived a deploy, a verification run and two
rounds of reporting the symptom as "the deploy has not landed". The symptom was real; the
diagnosis was wrong for as long as nobody read the log.

## Local evaluation hardware

Python 3.12.5, Docker 29.7.2, Node 20.14, **NVIDIA RTX 4050 Laptop, 6 GB VRAM**.

6 GB is the binding constraint on the analytics plane and is why the adaptive sampling
controller (§7.2) is load-bearing rather than a bonus: 30 cameras at full frame rate
will not fit, and the honest answer to "how do you reach 80,000?" is spending compute
where the evidence is, not assuming a larger GPU.
