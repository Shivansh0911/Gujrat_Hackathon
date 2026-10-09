# What changed since Phase 1, and what each change is worth

One page for the evaluation panel. Every figure here is measured and traceable to
`docs/DISCOVERY.md`, which records the failures alongside the results.

## The diagnosis Phase 2 was built on

Phase 1 ended at a wall every entrant hits: **ANPR accuracy of 29.6%**, and the ceiling
is not the recogniser. It is what the estate publishes. Measured at the source, a plate
occupies too few pixels on most of these cameras for any recogniser to read it.

Phase 2 does not attempt to beat that number. It stops requiring a readable plate for
every hop, and it reports the ceiling per camera instead of absorbing it into one
estate-wide figure. That reframe is what the work below delivers.

## What was built

| | |
|---|---|
| **Identity that survives an unreadable plate** | Sighting decoupled from plate read. A 76-dimension CIELAB-and-edge appearance fingerprint in a `pgvector` HNSW index. Candidate journeys anchored on one legible read. A three-tier evidence model — Confirmed, Probable, Candidate — that no control can promote between. |
| **The ceiling turned into a deliverable** | Per-camera ANPR readiness grading from measured pixel budget. Analytics assigned by capability rather than configuration. A Scale Planner that extrapolates measured per-camera cost to 80,000. |
| **Onboarding that shows its working** | A live staged probe: SSRF validation, timed TCP handshake, transport negotiation with its reason, codec and resolution, **declared against measured frame rate**, the first decoded frame, a readiness grade with reasoning. A stage never reached is drawn as skipped, not omitted. Plus a real ONVIF adapter. |
| **Integration surface** | Scoped service-account API keys, HMAC-signed webhooks with timestamp binding, idempotency keys, cursor pagination, API versioning, Prometheus metrics, structured logs with correlation IDs, AES-256-GCM sealed camera credentials. |
| **Analytics beyond ANPR** | Cloned-plate, convoy and dwell detection; corridor flow analytics; an operational dashboard. Each ships its limits in the response body beside its results. |
| **Forensic 3D** | Coverage as viewsheds. A time-as-altitude journey plot where loitering, convoy and impossible travel are shapes rather than computations. |
| **Operational readiness** | A tested backup/restore drill with audit-chain verification, and an on-call runbook for gateway down, camera flapping, database failover and disk pressure. |

## What each change is worth against the §9 criteria

**1. Successful test case.** The scored path is unchanged and still works; what is added
is that a camera too coarse for ANPR is no longer a dead end. It is graded, assigned
counting and appearance analytics it can support, and contributes sightings to a
candidate journey.

**2. Solution presentation.** The honesty posture is now interface rather than prose: a
"What am I looking at?" panel on every screen naming its limits, a permanent evidence-tier
legend, and an eight-step guided tour that runs against real screens and real queries.

**3. Solution architecture.** The federation boundary is now demonstrable rather than
described — ONVIF adapter, probe, scoped keys, signed webhooks. Security was extended
without relaxing anything: RLS, the SSRF guard, JWT validation and the append-only ledger
are untouched, and the probe endpoint — the one new outbound-connection surface — is
admin-only, SSRF-guarded, re-verified against DNS rebinding immediately before connecting,
and records host and port rather than a URL that may carry credentials.

**4. Working platform and demonstration.** Fifteen screens. The Onboarding Studio is the
strongest sixty seconds available: a camera declaring 25 fps and delivering 15, on screen,
is a more credible answer to the integration guide's own warning than any slide.

**5. Video analytics output.** ANPR, vehicle detection and classification, counting, flow,
dwell, intrusion zones, cloned-plate and convoy detection, appearance-based cross-camera
correlation. The appearance gate ships at a **measured false-link rate of 0.055**; a looser
gate measured **0.834** and was rejected (`DISCOVERY.md` finding 25).

**6. Scalability and PoC readiness.** The Scale Planner is arithmetic on measurements taken
on the stated hardware, with assumptions adjustable on screen. Readiness grading answers the
procurement question the ceiling actually raises: which cameras to re-aim, which to replace,
and which are fine for everything except plates.

**7. Submission completeness.** README, HLD and DISCOVERY are current; DISCOVERY carries
twenty-seven dated findings. A refreshed demonstration script, an on-call runbook, and a
recorded backup/restore drill result.

**Bonus.** Cross-camera correlation without plates; analytics beyond the mandate; auditability
extended to cover every accept/reject decision; integration-ready APIs with metrics and
runbooks.

## What is still not true, stated here rather than found later

- **ANPR accuracy is still 29.6%.** Phase 2 routes around the ceiling; it does not raise it.
- **No camera in the estate publishes a heading.** 491 cameras, 271 with a position, 0 with an
  orientation. Coverage therefore draws uncertainty domes, not viewshed cones. The cone
  geometry is built and waiting on survey data that does not exist.
- **Load-adaptive analytic sampling is not built.** The analytic rate is fixed at 5 fps. This
  is the largest gap between the scale model and the running system.
- **The ONVIF adapter has not met a physical ONVIF camera.** It is implemented and unit-tested
  against recorded device responses; no device was available.
- **The gateway media plane is third-party and intermittent, and its quota is per account.**
  A newly issued access code restores catalogue access and does not return watch time; the
  cooldown still has to elapse (`DISCOVERY.md` finding 28). Nothing in this repository can fix
  that, and the runbook's first job is to establish it quickly rather than attempt a repair.
  Every screen except the Control Room reads from the database and keeps working through it.
- **Appearance matching is a search aid, not an identification.** It narrows thousands of
  sightings to a handful a human adjudicates with both crops side by side.

## How this was verified, not just built

A scripted walkthrough signs in and visits all fifteen screens in the order a reviewer
meets them, asserting the *content* of each rather than that it rendered — a screenshot
proves a page drew, and an empty panel photographs just as well as a full one. Twenty-three
assertions, all holding, with the images in `docs/screenshots/phase2/`. A second script
exercises every section of the brief against the live API and checks the shape of each
answer: eighteen checks, all passing.

That is also how the Judge Mode defects were found. The button a reviewer presses first
turned out to pre-fill a registration with no rows in this database, navigate in a way the
router ignored, lose a race with the guided tour, discard three of its four query
parameters, and rank its chosen plate by a measure that selected a physically impossible
route. Each was invisible until the path was actually run.

## Quality gates

687 automated tests · `mypy --strict` clean · `ruff` clean · declared-FPS guard (exact
permitted count, not a per-site marker) · WCAG AA contrast check over the theme tokens ·
reversible migrations · gitleaks over full history · CycloneDX SBOM.
