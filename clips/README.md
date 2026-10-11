# Demonstration clips

Screen recordings of the **live deployment** — `setu-gujarat.netlify.app` against the
Render API. Not a mock-up and not a local instance: what the clip shows is what a judge
opening the URL sees.

H.264 in MP4, 1600×900, no audio, ~95 seconds each. PowerPoint plays them directly:
**Insert → Video → This Device**, then **Start: Automatically**.

Two things are done deliberately for a projected room: a blue cursor ring is drawn into
the recording so the audience can follow what is being clicked, and **panels are zoomed
while they are being discussed** — a dashboard tile that is legible on a laptop is not
legible from the back of a hall.

Run them in order. Each is one idea.

---

### 1 · `01-coverage-what-the-estate-sees.mp4`
Coverage answers *where can the state actually see, and what can it do there*. The clip
zooms the readiness legend, flies the map into Ahmedabad so the domes read as areas
rather than dots, and steps through all four analytics modes.

> *"Each camera is drawn as the ground it can actually see. They are domes, not cones,
> because not one camera in this estate publishes a heading — a cone would claim an
> orientation nobody surveyed."*

### 2 · `02-dashboard-live-estate.mp4`
Centralised monitoring, panel by panel: **40 vehicles seen against 20 plate reads**, an
hourly rate and peak, the window switched to 7 days, class mix, camera health, anomalies,
cameras needing attention.

> *"We count what the detector saw, not what the OCR managed to read — and each panel
> states its own limit. A pass at two cameras counts twice: these are traffic events, not
> distinct vehicles."*

### 3 · `03-USP-find-a-vehicle.mp4` — **the USP**
> **40 vehicles, of which 0 also produced a readable registration.**

> *"Not one vehicle on this estate can be found by a plate-based system. Every other
> entrant's search returns nothing here. Ours returns all forty."*

The purpose is typed into a field marked *recorded in the audit ledger before the search
runs* — the reason is written down **before** any result exists. Then a vehicle is picked
and the estate searched by appearance alone; every result is tagged `Candidate`.

### 4 · `04-journey-trace-and-evidence.mp4`
One click from the login screen gives **4 hops · 413.4 km · 340 minutes**. The clip zooms
each hop card in turn — camera, timestamp, tier badge, ANPR confidence, position
uncertainty, and the evidence crop itself — then the coverage-gap line and the signed
export.

> *"24 cameras on this route produced no detection — a coverage gap, not an absence of
> the vehicle. And 8 cameras were excluded outright: no coordinate on record."*

### 5 · `05-shape-of-this-movement-3d.mp4` — the signature view
The same journey with **geography across and time upward**, orbited and then scrubbed.

> *"A vertical segment is a vehicle that stayed put while time passed. A near-horizontal
> one is distance crossed with almost no time — the signature of a cloned or misread
> plate, not a fast car. Two routes parallel at the same altitude were travelling
> together."*

### 6 · `06-registry-alerts-health.mp4`
The registry (Model 1, the mandatory foundation) with the map flown in and a camera
selected; the alert desk with a match opened; and Health showing **declared frame rate
against measured**.

> *"The integration guide warns that cameras over-report their frame rate. We measure it
> and show both — a camera claiming 25 fps and delivering 15 is reported, not trusted."*

### 7 · `07-control-room-live.mp4`
The multi-camera wall with **live government feeds playing** — 2×2, then 2×3, then a
single full-screen tile. The footage carries the camera's own timestamp overlay, so there
is no question about whether it is live.

> *"These are the department's own cameras, streamed through the platform. The wall is
> the operator's view; everything else in this demonstration is what the platform does
> with what those cameras saw."*

### 8 · `08-onboarding-camera-and-department.mp4`
A camera connecting, with **every stage shown as it happens**: URL accepted → host
reachable → transport chosen → stream opened → frame rate → first frame decoded → ANPR
readiness → analytics assigned. Then a **department being added** — code, name, submitted
— beside the counts already onboarded.

> *"A failure that says only 'could not connect' is useless to whoever has to fix it.
> This tells them whether it is a firewall, a password or a codec. And twenty-six
> departments cannot each need an engineer on our side."*

### 9 · `09-blindspots-watchlist-zones.mp4`
Gap analysis by district, separated by remedy. The watchlist, where the **authorisation**
for an alert lives and when it expires. Intrusion zones on the map.

> *"A watch without a recorded reason and an expiry is surveillance, not policing."*

### 10 · `10-scale-system-demo.mp4`
The Scale Planner — 80,000 cameras, every constant traceable to a measurement, inputs
editable so a judge can argue with it. Then the audit ledger **verified live**, and the
own-feed clip frame-aligned to the reads it produced.

> *"Each entry's hash includes the one before it. You cannot edit history here without
> every later hash disagreeing — and the screen re-checks the whole chain on demand."*

---

## About the Control Room

The gateway was down for most of this project's life, and the clip above was re-recorded
once it came back. Two things have to be true for tiles to play: the current access code
must be set on the API, and the organiser's watch-time quota must not be spent. Both held
at recording time.

If the wall is down on the day, it is worth showing rather than hiding. Each tile names
the error, the camera and its last known status, and says that the registry entry,
coordinates and recorded detections for that camera remain available. The control plane
and the media plane fail independently by design, and every other screen in this pack
reads from the database — which is why the running order leads with them.

## If the venue network is hostile

The basemap loads OpenStreetMap raster tiles. A filtered network can block them, leaving
a grey background with the route and markers still drawn. **These clips are recorded video
and play regardless** — that is their other job. Test on the venue network beforehand and
carry a hotspot.
