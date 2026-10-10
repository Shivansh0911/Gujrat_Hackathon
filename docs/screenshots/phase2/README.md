# Phase 2 console, in the order a reviewer meets it

Captured in Edge at 1680×1050 against a live backend, by a scripted walkthrough that
asserted the *content* of each screen rather than only that it rendered — a screenshot
proves a page drew, and an empty panel photographs just as well as a full one. Every
assertion held on the run these images come from.

Edge rather than bundled Chromium throughout: the bundled build ships no H.264 decoder,
so the Control Room tiles render black in it and every screenshot of it would lie.

| | Screen | What it is there to show |
|---|---|---|
| 01 | Login | Role selection, and Judge Mode — the scored path one click from here |
| 02 | Signed in | The guided tour opening itself on a first visit, now centred on the right edge |
| 03 | Find a vehicle | **The capability that does not depend on ANPR.** 48 vehicles the detector recorded, of which 1 has a readable plate — the other 47 are invisible to every plate-based search |
| 04 | Coverage | The login destination. Every placed camera as the ground it can see, and why they are domes: no camera in this estate publishes a heading |
| 05 | Coverage, dark | The same screen in the other theme |
| 06 | Dashboard | Vehicles seen before plate reads — 402 against 20 — with the class mix, camera health and anomalies carrying their limits |
| 07 | GIS Map | The camera registry — Model 1, the mandatory foundation |
| 08 | Journey, empty | The designed empty state, not a blank panel |
| 09 | Journey, traced | 4 hops, 413 km, with evidence crops, tiers, appearance candidates and the signed export |
| 10 | Journey, time as altitude | Geography across, time upward, now ruled at round clock intervals so a climb reads as a duration |
| 11 | Journey, scrubbed | The same trace replayed to the halfway point |
| 12 | Alert Desk | Watchlist matches, including the fuzzy match an exact system misses |
| 13 | Control Room | The multi-camera wall |
| 14 | Onboarding Studio | Paste a source, or import a department |
| 15 | Onboarding, probed | Every stage of the probe, including the ones it never reached |
| 16 | Scale Planner | 80,000 cameras, every constant's provenance, and the operational half — scaling, monitoring, backup, DR, security |
| 17 | Health | Declared against measured frame rate |
| 18 | Blind spots | Gap analysis, separated by remedy |
| 19 | Watchlist | Where the authorisation for an alert lives, with expiry |
| 20 | Zones | Intrusion detection areas |
| 21 | System | The hash-chained audit ledger, verified |
| 22 | Demo | The own-feed clip, frame-aligned to the reads it produced |
| 23 | Command palette | Ctrl/⌘-K |
| 24 | Shortcuts | The keyboard reference, on `?` |

## Two things these images show that are not defects

**The Control Room tiles and a 502.** The gateway's watch-time quota was exhausted when
these were taken. The catalogue answers normally — the control plane and the media plane
fail independently, and `DISCOVERY.md` finding 28 records that a newly issued access code
restores the first and not the second. Every screen except the Control Room reads from
the database and is unaffected, which is why the demonstration runbook leads with them.

**Nearly every camera reads "not yet graded".** Readiness grading requires eight plate
reads from a camera before it will assign a grade, and most of this estate has produced
fewer. That threshold is deliberate and was itself a correction: an earlier version
graded five readable plates at 87 px as `not_viable`, which is weak evidence of
capability being reported as evidence against it.
