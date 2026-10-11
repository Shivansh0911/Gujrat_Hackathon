# The deployed console, in the order a judge meets it

Captured **2026-10-11**, re-captured after the gateway came back against `https://setu-gujarat.netlify.app` and
`https://setu-api-8m2h.onrender.com` — the live deployment, not a development
instance — by a scripted walkthrough that asserted the **content** of each screen
rather than that it rendered. A screenshot proves a page drew; an empty panel
photographs exactly as well as a full one, which is the whole lesson of the four
runs before this one.

**Every assertion held. Zero requests returned 400 or above.**

Edge rather than bundled Chromium throughout: the bundled build ships no H.264
decoder, so the Control Room tiles render black in it and every screenshot of that
screen would lie.

| | Screen | What it is there to show |
|---|---|---|
| 01 | Login | Role selection, and Judge Mode — the scored path one click from here |
| 02 | Judge Mode, one click | **4 hops · 413.4 km · 340.0 min**, from signing in and nothing else. Three-tier legend, signed export, four decoded evidence crops |
| 03 | Time as altitude | The signature view. Geography across, time upward, ruled at round clock intervals so a climb reads as a duration |
| 04 | Rotated and pitched | The same plot under interaction — it is a WebGL layer in the map's own scene, not a picture of one |
| 05 | Scrubbed to halfway | The trace replayed against the time axis |
| 06 | Back on the map | The route survives the return |
| 07 | Dashboard | **40 vehicles seen against 20 plate reads**, an hourly rate, class mix, busiest cameras, camera health, anomalies carrying their limits |
| 08 | Find a vehicle | **The capability that does not depend on ANPR.** 40 vehicles, of which 0 produced a readable registration — every one invisible to a plate-based search |
| 09 | Coverage | Every placed camera as the ground it can see, and why they are domes: no camera in this estate publishes a heading |
| 10 | GIS Map | The camera registry — Model 1, the mandatory foundation |
| 11 | Journey, empty | The designed empty state, and the form that refuses to run without a stated purpose |
| 12 | Alert Desk | Watchlist matches, including the fuzzy match an exact system misses |
| 09 | Control Room | The multi-camera wall, **with live government feeds decoding** |
| 14 | Onboarding Studio | Paste a source, or import a department |
| 15 | Scale Planner | 80,000 cameras, every constant's provenance, and the operational half |
| 16 | Health | Declared against measured frame rate |
| 17 | Blind spots | Gap analysis by district, separated by remedy |
| 18 | Watchlist | Where the authorisation for an alert lives, with its expiry |
| 19 | Zones | Intrusion detection areas |
| 20 | System | The hash-chained audit ledger, verified live |
| 21 | Demo | The own-feed clip, frame-aligned to the reads it produced |
| 22 | Command palette | Ctrl/⌘-K |
| 23 | Shortcuts | The keyboard reference, on `?` |
| 24 | Coverage, dark | The second theme — designed, not inverted |

## Three things in these images that are not defects

**The Control Room now plays.** The government gateway was down for most of this
project and these tiles showed the failure state. With a current access code set on the
API and the organiser's quota unspent, they decode live footage carrying the camera's own
timestamp. Both conditions have to hold; if the wall is down on the day, each tile still
names the error and states that the registry, coordinates and recorded detections remain
available.

**"0 of 40 produced a readable registration" on Find a vehicle.** That is not a
failure of the screen; it is the argument the screen exists to make. Not one vehicle
on this estate can be found by a plate-based system.

**Nearly every camera reads "not yet graded".** Readiness grading requires eight
plate reads from a camera before it will assign a grade, and most of this estate has
produced fewer. That threshold was itself a correction: an earlier version graded
five readable plates at 87 px as `not_viable`, which is weak evidence of capability
being reported as evidence against it.

## Reproducing this

The walkthrough is not committed, because a browser script pinned to one deployment
goes stale faster than it earns its place. What it asserted is recorded as
`DISCOVERY.md` finding 42, and the numbers in this index are the ones it checked.
