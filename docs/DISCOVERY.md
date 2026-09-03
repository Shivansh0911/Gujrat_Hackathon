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

## Local evaluation hardware

Python 3.12.5, Docker 29.7.2, Node 20.14, **NVIDIA RTX 4050 Laptop, 6 GB VRAM**.

6 GB is the binding constraint on the analytics plane and is why the adaptive sampling
controller (§7.2) is load-bearing rather than a bonus: 30 cameras at full frame rate
will not fit, and the honest answer to "how do you reach 80,000?" is spending compute
where the evidence is, not assuming a larger GPU.
