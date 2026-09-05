# SETU — demonstration runbook

**Updated 2026-09-05.** The government gateway is reachable again and the figures below
are the current ones — the estate moved host on 1 September and added an email to its
sign-in on 3 September, so anything you remember from an earlier read of this file is
probably out of date.

Everything in section 2 still works with the gateway unreachable, which is the state to
plan for: it went down twice during the build and once mid-session.

---

## 0. The live deployment

Everything below describes running SETU locally. If you only need to *show* it, the
deployed instance is already running and already seeded.

| | |
|---|---|
| **Console** | https://setu-gujarat.netlify.app |
| **API docs** | https://setu-api-ai7z.onrender.com/docs |
| **Accounts** | `admin` (System Administrator) · `operator` (Control Room Operator) |
| **Passwords** | in the gitignored `deploy-secrets.env` at the repository root |

**The passwords are deliberately not written into this file.** This document is
committed and pushed; a working credential in a public repository is a secret leak
regardless of why it was put there. They belong in two places only: the submission
form, and `deploy-secrets.env` on a team machine. `cat deploy-secrets.env` when you
need them.

Two things to know before clicking:

- **The first request after a quiet spell takes about a minute.** Render's free tier
  sleeps after 15 minutes idle. An UptimeRobot monitor pings `/healthz` every 5
  minutes to prevent it, but open the link yourself once before a demonstration
  rather than discovering it in front of an audience.
- **Use `operator` for at least one screen.** It cannot add watchlist entries,
  onboard cameras or run catalogue reconciliation, and showing that refusal is worth
  more than describing the role model.

---

## 1. Start from a clean checkout

```bash
make venv          # virtualenv + pinned dependencies
cp .env.example .env
```

Then generate real secrets into `.env` — the API refuses to issue tokens against the
placeholder values, deliberately:

```bash
python - <<'PY'
import secrets, pathlib
p = pathlib.Path(".env"); t = p.read_text()
for key in ("POSTGRES_PASSWORD", "SETU_JWT_SECRET",
            "SETU_ADMIN_PASSWORD", "SETU_OPERATOR_PASSWORD"):
    t = t.replace(f"{key}=change-me-locally", f"{key}={secrets.token_urlsafe(24)}")
p.write_text(t)
print("secrets generated")
PY
```

Set `SETU_DATABASE_URL` to match the generated `POSTGRES_PASSWORD`.

```bash
make demo
```

That brings up Postgres, migrates, seeds the registry and coordinates, ingests the
own-feed footage across the replay cameras, seeds the watchlist, raises alerts, and
builds the console. It takes roughly four minutes, most of it ANPR inference.

Then, in two terminals:

```bash
make api            # http://127.0.0.1:8090/docs
make frontend-dev   # http://localhost:5173
```

**Credentials:** username `admin`, password is `SETU_ADMIN_PASSWORD` from `.env`.

### Hosted instance (for the screening committee)

| | |
|---|---|
| URL | _to be filled once deployed — see `docs/DEPLOYMENT.md` §4_ |
| Operator account | `operator` / _record the deployed value_ |
| Admin account | `admin` / _record the deployed value_ |

Credentials are generated per deployment and are never committed. Record the live
values here before submitting.
There is also an `operator` account with the read-only role.

---

### Automating a first draft

`make record-demo` drives this section beat by beat with Playwright and writes an
unnarrated WebM to `docs/demo-recordings/`. `make record-demo-gateway` does the same
against government-feed data after `make gateway-ingest`.

**This is a first draft, not the deliverable.** It exists so the framing, ordering and
timing are already right before anyone records for real. It has no narration, and the
"say this" lines below are what carry the argument — silent footage of a map
demonstrates nothing. Watch the output, then either narrate over it or re-record with
narration. The submission checklist item for the two demonstration videos is **not**
satisfied by running it.

The script fails loudly if a screen it needs is empty — an alert desk with no alerts,
a journey with no hops — because those are exactly the states that look like broken
software on camera.

## 2. What to demonstrate, screen by screen

### 2.1 GIS Map — 40 seconds

Open on **GIS Map**. The point to make is not that pins exist; it is that the map
tells the truth about how well we know each position.

- 30 cameras across Ahmedabad, Junagadh, Rajkot, Patan, Navsari and Kutch.
- **Say this:** *"Nine of these cameras we can only place to a district. We draw them
  as a circle at their real uncertainty rather than as a precise pin, because a false
  pin produces a route that looks authoritative and is wrong."* The legend states it.
- Scroll the side panel to **`coordinate missing` — 2 cameras** (`20 Mohanpura`,
  `30 kheram`). These are excluded from every spatial query and are listed rather
  than hidden.
- Click **Place pin** on one, click the map. The coordinate is written as
  `manual_survey` against your account with an audit entry, and takes effect
  immediately with no redeploy.
- Click any camera to open the detail panel: declared versus measured fps, codec,
  transport, provenance.

### 2.2 Journey View — the scored capability, 2 minutes

This is the screen the live test case is judged on. Spend the time here.

> **The plate is `KA25AB1542`, and it is the vehicle's real registration.** Local and
> deployed instances now ingest the same clip and produce the same reads, so this no
> longer varies between machines. If you replace the footage, re-read it off the Alert
> Desk or take it from check 3 of `verify_deployment.py`.

**Type plate:** `KA25AB1542`
**Purpose:** `FIR 123/2026 — vehicle trace requested by Investigating Officer`
Leave the default time window. Press **Trace vehicle**.

Expect **4 hops, ~413 km, 340 minutes, query under 100 ms**.

Points to make, in order:

1. **The purpose field is mandatory and is written to the tamper-evident audit ledger
   *before* the query runs.** A search that returns nothing is logged exactly like one
   that returns a route — otherwise the most sensitive case, someone searching a plate
   they should not, would be the one case that leaves no trace.
2. **Every hop carries its evidence crop.** The image is what a reviewer checks the
   read against.
3. **Provenance badges.** Green `ANPR confirmed`; amber
   `ANPR partial — N chars corrected` with the exact substitutions on hover. A
   corrected plate is never presented as clean.
4. **Position confidence per hop** — `±1.5 km`, `±4.0 km`. Plausibility gating widens
   its tolerance in proportion, so an approximate camera is not falsely rejected
   against a precise neighbour.
5. **Implied speed column** — 70 km/h, 77 km/h. A reviewer can check the physics
   independently rather than taking the route on trust.
6. **The dashed footer: `13 camera(s) on this route produced no detection`.** This is
   the line worth pausing on:
   > *"Every system in the room will show you where it found the plate. Ours also
   > shows you where it didn't — and tells you which of those gaps is a coverage
   > problem you should fix."*
7. **`2 cameras excluded from this search: no coordinate on record.`** We report what
   we could not consider.
8. **Press `Export signed evidence (PDF)`.** A three-page document downloads: the
   query and its authorisation, every hop with its crop and all three clocks, then
   coverage gaps, rejected candidates, and an integrity page carrying an Ed25519
   signature over a manifest that commits to every hop and to a SHA-256 of each
   evidence image. **Say this:** *"A recipient can verify this with the public key
   alone. They do not need access to our database, our API, or us."* The export is
   itself audited — producing a distributable evidence document is a more
   consequential act than looking at a route on screen.

**Then demonstrate the negative case.** Type `MH99ZZ9999`, same purpose, trace. The
result distinguishes *plate never seen anywhere* from *seen, but not in this window*
— different findings, and an investigator needs to tell them apart.

### 2.3 Alert Desk — 60 seconds

**8 alerts**, all grouped as movement alerts.

- **Click a `fuzzy_1` card** — that is the one to demonstrate. The watchlist holds a
  near-miss of a plate present in the footage, differing by one confusion-pair
  character (`G`/`6`, `I`/`1`, `B`/`8`). **An exact-match system finds nothing here.**
  The card shows the match type and the reduced score.

> **On accuracy, get in first.** Plate-level accuracy is 29.6% precision and recall,
> 26.9% character error rate, measured against a by-eye annotation of every evidence
> crop. It was 0% until three defects were found by measuring: a recogniser with nine
> character slots when Indian plates have ten, track association that never associated
> anything so multi-frame fusion never ran, and fusion that right-aligned reads of
> different lengths and voted unrelated characters against each other.
>
> **Say that, rather than waiting to be asked.** It is the strongest thing in the
> submission: the platform measures its own output, and the measurement found real
> bugs that reading the code did not. Then point at the confidence column — a read
> below 0.5 is not published at all, because a wrong registration carrying a high
> confidence is the one an investigator would act on.

### 2.4 Coverage — gap analysis, 45 seconds

Model 1's own requirement, and the screen that turns the registry from an inventory
into a planning tool.

- Point at the **district confidence bars**. Ahmedabad 74%, Gandhinagar 60%, and the
  finding beside each one naming why.
- **Say this:** *"Gaps are separated by remedy, because the cost of each differs by
  orders of magnitude. A missing coordinate is a pin drop. An approximate one needs a
  survey. A degraded camera needs maintenance on money already spent. Uncovered ground
  needs procurement."*
- Scroll to **Investigation-derived gaps**. These are positions that real plate
  queries kept needing, where nothing was recorded — 29 of them. That is an
  evidence-backed case for where the next camera goes, not an opinion about it.
- The red circles on the map are those positions, sized by how often investigations
  needed them.

### 2.5 Health — 40 seconds

- Sort by **fps drift**. Declared versus measured frame rate side by side.
  **Say this:** *"The integration guide warns that a camera's reported frame rate
  cannot be trusted. On this estate, 20 of 30 cameras declare no rate at all. Every
  measured figure here comes from stream presentation timestamps."*
- Click **Fault report** on any camera. It generates the organiser's §2.5 payload
  verbatim — camera id, exact URL, client and version, UTC timestamp, client-side log
  — ready to send.

### 2.6 Watchlist — where the authorisation lives, 30 seconds

Show this straight after the Alert Desk, because it answers the question every alert
provokes: *who decided to watch this vehicle, and for how long?*

- The table names the authority and case reference behind each entry, and the
  **remaining life** of the listing.
- **Say this:** *"Expiry is a required field. There is no option to list a vehicle
  indefinitely — an entry with no end date is a permanent record about a citizen,
  created by omission, so the API refuses one."* Open **Add vehicle** to show the form.
- Adding is admin-only and written to the audit ledger before it takes effect. Sign in
  as `operator` if you want to show the control disappear.

### 2.7 System — integrity, 20 seconds

Press **Verify now**. The chain re-hashes and reports `chain intact`, the number of
entries checked, and the head hash. Every journey query, every pin drop, every alert
acknowledgement and every failed login is in there, each entry committing to the one
before it.

**Say this:** *"Tamper-evidence only a developer can check is a claim, not a control.
This is the same verification, in front of the officer who depends on it."*

The same screen reconciles the registry against the gateway catalogue and reports the
difference without applying it — a camera dropping off a third-party feed for ten
minutes is not authority to delete its identity or its evidence.

---

## 2.8 The government-feed recording — the second required video

The organisers require two recordings: one on our own footage, one on the government
feed. Section 2 covers the first. This is the second, and it is a different film with a
different argument.

**Do not try to make it look like the first one.** The estate does not deliver
readable plates, and a recording that implies otherwise will not survive a question.
The argument this video makes is: *we connected to the real feed, at estate scale, and
we can tell you precisely what it does and does not give us.* That is a stronger
position than a polished demo a judge can puncture in one question.

### Before recording — the feed is unreliable

```bash
make gateway-ingest        # ~25 minutes across all 30 cameras
make gateway-report        # merges passes into the output report
```

Check `reports/evidence/gateway-output-report-*.md` for the camera list. Availability
changes by the hour: cameras 17 and 18 have returned HTTP 500 consistently, three
others time out. **Re-run the ingest the morning of the recording** and use the report
it produces, not the numbers below.

### The sequence — about 3 minutes

| # | Show | Say | Time |
|---|---|---|---|
| 1 | **Health screen** | *"Twenty-five of thirty cameras deliver frames. We report that per camera, because a camera's own live flag is a claim, not a health signal."* | 40 s |
| 2 | Same screen, measured frame rate | *"Every one of these rates was measured by decoding the stream. The catalogue publishes none — it is 1,373 bytes for thirty cameras, an id and a name. Nothing in our pipeline reads a declared frame rate; every timestamp comes from stream PTS."* | 30 s |
| 3 | **Map**, gateway cameras | *"Twenty-four of the thirty placed, and drawn as a confidence circle wherever we can only place them to a district. Six resolve to nothing and are shown as having no position rather than guessed."* | 25 s |
| 4 | **Alert Desk**, the `cam22` crop | *"Three thousand nine hundred frames across twenty-five live cameras. Eighteen plate regions. One grammar-valid registration — GJ09BM3641, here it is, and here is the crop it came from."* | 40 s |
| 5 | The same alert, showing it is an intrusion | *"That plate arrived through a zone we drew on that camera's own view. Live ingest, stored detection, classifier, alert — nothing tuned for it. Intrusion detection is the analytic that works on this estate today, because it needs a vehicle box rather than a readable plate."* | 40 s |
| 5b | The `cam03` and `cam02` crops | *"And these two are not vehicles at all — a camera's own caption, and a building facade. We left them in. Deleting the outputs that embarrass you is how an error rate stops meaning anything."* | 30 s |
| 6 | **Coverage** | *"So the finding we would give Gujarat Police is not 'our recogniser needs tuning'. It is that this estate publishes below the resolution at which any recogniser can read a plate. That is a procurement and placement finding, and it is the argument for processing at the edge where full resolution still exists."* | 30 s |

### Why lead with the failure

Because a technical jury will find it within one question, and the difference between a
team that measured its own accuracy and published it and a team that did not is the
whole of the credibility on offer. Everything else in the submission — the audit chain,
the signed evidence, the tenant isolation — asks to be trusted. This is the part that
earns it.

The recogniser is one component behind an interface (ADR 0003), a better-scoring
candidate is already identified and measured, and swapping it does not touch the
architecture. Say that too.

---

## 3. If the gateway is refusing you on the day

It has done so twice, in two different ways, and neither was a 502 in the end. On
31 August the origin returned Cloudflare 502s on every endpoint. On 3 September the
estate added an email field to its sign-in and locked RTSP behind credentials in the
same change — the symptom was our own console reporting the gateway unreachable while
the estate was perfectly healthy. **If the feed is refusing you, check the Health
screen's gateway card first: it records what the last error actually was and when
contact was lost.** **Nothing in section 2 depends on it**, and the platform now says
so on screen rather than leaving you to explain it.

### What is actually measured

Do not describe this vaguely — the numbers are recorded and they make the point better
than an apology does:

| Date | Estate | Cameras producing frames | Valid registrations read |
|---|---|---:|---:|
| 2026-08-27 | `live.corp8.cloud` | 25 of 30 | 2 |
| 2026-08-30 | `live.corp8.cloud` | 18 of 30 | 0 |
| 2026-08-31 | `live.corp8.cloud` | **0 of 30** — Cloudflare 502 on every endpoint | — |
| 2026-09-02 | `cctv.corp8.cloud` | 22 of 30 | 0 |
| 2026-09-03 | `cctv.corp8.cloud` | 25 of 30 | **1** — `GJ09BM3641` |

Same pipeline on all five days, across two different estates. The only thing that
changed was the feed.

### What to say, in about twenty seconds

Open **Health**. The card at the top of the page states the gateway's status, and if it
is down, when it went down and how long ago. Then say roughly this:

> "The government gateway is unreachable right now — our health page shows it went
> down at *[time on screen]*. That's the organiser's origin server; Cloudflare is
> answering, the host behind it isn't. What you're seeing below is our own recorded
> data, which is unaffected. We designed for this: the feed has been between 25 and 0
> of 30 cameras across the last week, so the platform treats gateway availability as
> something to report rather than depend on."

Then carry on with the demonstration. Do not apologise repeatedly, and do not go
looking for a workaround mid-demo — there isn't one, because the fault is not ours.

### Why this reads as strength rather than excuse

Three things are true at once, and saying all three is what makes it credible:

1. **It is visibly handled.** The status card is on screen before anyone asks, with a
   real timestamp. A platform that discovers its dependency is down only when someone
   clicks a button has not thought about operations.
2. **It is bounded.** Only live camera preview and fresh ingestion need the gateway.
   Registry, GIS, journey reconstruction, alerts, audit and evidence export all read
   from data already recorded, and all of them work with the feed completely dark.
3. **It is documented, and was reported.** `docs/SUPPORT_QUERY.md` is the fault report
   prepared for the organisers, with DNS and port-reachability evidence attached.

### What the platform does under the hood

Worth one sentence if asked, not more: each camera session backs off exponentially
with **full jitter**, capped, so a whole-domain outage does not turn ~50 workers into a
thundering herd against infrastructure we do not own. A full-domain 502 and a single
camera failing take the identical path — connect fails, back off, retry — which is why
no special case was needed for this outage.

The passive watcher polls once a minute using the same catalogue call the ingest path
uses, so "reachable" means the same thing everywhere. It logs an outage **once**, at
the transition, rather than every minute.

### If it recovers mid-demo

The card flips to reachable on the next poll, within a minute. To capture fresh
evidence:

```bash
make gateway-ingest && make gateway-report
```

Do not do this during the demonstration itself — a full sweep takes about twenty
minutes.

---

## 4. Honest limitations — state these before a judge finds them

Saying these first is what makes everything else in the submission credible. A jury
that discovers a limitation itself discounts the whole; a jury told the limitation up
front tends to trust the rest.

1. **ANPR plate-level accuracy is 29.6% precision and recall**, 26.9% character error
   rate, measured against a by-eye annotation of every evidence crop
   (`data/seed/anpr_ground_truth.csv`). Four of the seven government-feed crops from
   camera 7 are read exactly right — that camera belonged to the estate retired on
   1 September and has no equivalent in the current one, so quote it as history rather
   than as something a judge can go and look at.

   It was **0%** when first measured. Three defects were found by measuring rather
   than by reading code: a nine-slot recogniser when Indian plates have ten
   characters, track association that never associated anything so multi-frame fusion
   was dead code, and fusion that voted misaligned characters against each other. All
   three are fixed and covered by regression tests.

   **If asked "is that good enough?"** — no, and we say so. Resolution bounds it: the
   same pipeline reads 8 plates at 2560x1440 and none at 704x396, and only one
   government camera frames plates at a readable size. The recogniser sits behind an
   interface (ADR 0003) and is replaceable; the platform around it — federation,
   evidence chain, audit ledger, journey reconstruction — is what runs end to end on
   live government feeds.

2. **The own-feed clip is third-party** — a CC BY 3.0 Wikimedia clip of the
   Hubli-Dharwad BRTS route, attributed in `data/own_feed/SOURCE.md`. Being Karnataka
   footage, plates read `KA...`/`KL...` rather than `GJ...`.

3. **The four `REPLAY-...` cameras are a replay harness, not live feeds.** The
   government multi-camera feed does not give us one vehicle across several cameras,
   so route reconstruction is demonstrated by running the full pipeline separately
   against four registry positions. Every detection is genuine inference with a real
   crop; nothing is copied between cameras and no plate is invented. What is simulated
   is *which camera saw it*, and the `REPLAY` prefix is visible on screen.

4. **The gateway is unreliable and was unreachable for most of the build.** It
   recovered on 2026-08-27, partially: cameras 17 and 18 return HTTP 500 repeatably
   and several time out, and which cameras work changes hour to hour.
   `docs/SUPPORT_QUERY.md` is the fault report we prepared. **Re-run
   `make gateway-ingest` on the morning of the recording** and use the figures it
   produces rather than any number written down here.

5. **There is no hosted URL unless one has been deployed since.** The container stack
   is verified against nine checks; the deployment needs the team's platform account.
   `docs/DEPLOYMENT.md` §4.

6. **Face recognition is not built.** Deliberately. It stays unbuilt until all four
   governance controls fit — off by default per camera, recorded authorisation with a
   named officer and expiry, gallery scoped to an authorised case, separately
   auditable. An ungoverned biometric feature is worse than none.
