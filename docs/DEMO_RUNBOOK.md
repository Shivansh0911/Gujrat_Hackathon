# SETU — demonstration runbook

Everything here works with the government gateway unreachable, which is the state it
has been in for most of development and the state to plan for on the day.

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

> **Read the plate off your own instance before recording.** The demo ingest runs
> against whatever clip is bundled, so the exact registration differs between
> deployments — it was `KA25AB144` on the development stack and `KA25A3141` on the
> container stack. Open the Alert Desk first and take the plate from a card, or run:
>
> ```bash
> python backend/scripts/verify_deployment.py http://localhost:8080
> ```
>
> which prints the plate it successfully traced in check 3. **A demonstration that
> opens with "no route found" because the plate was copied from a document is the
> worst possible first thirty seconds.**

**Type plate:** the one you just read
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

- **Click the `fuzzy_1` card** — that is the one to demonstrate; on the development
  stack it was `KA25AI11G`, but as with the journey plate, **read it off your own
  instance first.** The detection read `KA25AI116`, the watchlist holds `KA25AI11G`,
  and `G`/`6` are a known OCR confusion pair. **An exact-match system finds nothing
  here.** The card shows the match type and the reduced score.

> **If a judge presses on this one, concede it cleanly.** Ground-truth annotation
> shows that detection is itself wrong — the vehicle is `KA25AB1116`, so the
> recogniser misread `B` as `I` as well. What the card demonstrates is real and worth
> demonstrating: confusion-aware matching recovers a vehicle that exact matching drops
> entirely. But it recovered it to a registration that is still wrong, and that is
> precisely why the platform shows the crop, the corrections and the score instead of
> presenting a plate as fact. **The reviewer is given what they need to disagree with
> the machine.** Do not let this be discovered rather than offered.
- Note the three timestamps on each card: `observed_at_utc`, stream `PTS`, and the
  read confidence. PTS is what makes a detection reproducible from the original
  stream rather than merely displayed.
- **Acknowledge** it, then **Resolve → False positive**. Explain that the disposition
  feeds the per-camera false-positive rate on the Health screen — the platform
  measuring its own precision rather than asserting it.
- **Discrimination matters:** the watchlist holds 9 entries. Five are valid
  registrations from Maharashtra, Delhi, Rajasthan, Tamil Nadu and Uttar Pradesh that
  are **correctly ignored**. A demo where everything matches is not believable.

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

### 2.6 Audit — 20 seconds

In the API docs (`/docs`), call **`GET /audit/verify`**. It recomputes the whole hash
chain and returns validity. Every journey query, every pin drop, every alert
acknowledgement and every failed login is in there, each entry committing to the one
before it.

---

## 2.7 The government-feed recording — the second required video

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
| 1 | **Health screen**, sorted by drift | *"Twenty-five of thirty cameras deliver frames. Two return HTTP 500, three time out. We report that per camera, because a camera's own `live` flag is a claim, not a health signal."* | 40 s |
| 2 | Same screen, declared vs measured | *"Five of the eight cameras that declare a frame rate are wrong about it — this one declares 12.5 and delivers 5.4. That is why nothing in our pipeline reads a declared frame rate. Every timestamp comes from stream PTS."* | 30 s |
| 3 | **Map**, gateway cameras | *"These are the live government cameras, placed where we can place them and drawn as a confidence circle where we can only place them to a district."* | 20 s |
| 4 | **Alert Desk / detections**, camera 7 crop | *"Nine thousand frames across twenty-five live cameras. Thirty plate regions. Three crops with a plate a human can read — all the same vehicle, on camera 7. Here it is."* | 40 s |
| 5 | The `GJ14AK5333` crop, enlarged | *"Our system read this as GJ14AK533 at confidence 0.94. It dropped a digit. We know that because we annotated every crop by hand and scored it: plate-level precision is zero, character error rate 39.8%."* | 40 s |
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

## 3. If the gateway is 502 on the day

It probably will be. Nothing above depends on it.

- The **live HLS player** on the camera detail panel will show
  `Live feed unavailable — upstream returned HTTP 502` with the camera's last known
  status. This is deliberate: a spinner that never resolves looks like our defect
  rather than theirs. Say so plainly and move on.
- **Everything else works**: registry, GIS, journey, alerts, health, audit — all read
  from data already ingested.
- `docs/SUPPORT_QUERY.md` contains the fault report already prepared for the
  organisers, including the DNS and port-reachability evidence.

If the gateway **does** recover, run `python backend/scripts/probe_catalogue.py --sequential
--emit-evidence` to capture live properties, then extend ingest across the catalogue.

---

## 4. Honest limitations — state these before a judge finds them

Saying these first is what makes everything else in the submission credible. A jury
that discovers a limitation itself discounts the whole; a jury told the limitation up
front tends to trust the rest.

1. **ANPR plate-level accuracy is 0.0%, character error rate 39.8%.** Measured against
   a by-eye annotation of all 80 evidence crops, committed as
   `data/seed/anpr_ground_truth.csv`. No registration has been read correctly on this
   footage. Two measured causes: the estate publishes below the resolution at which
   plates survive (the same pipeline reads 8 plates at 2560x1440 and none at 704x396),
   and model fit — a scored comparison puts `cct-s-v2` ahead of the configured
   `cct-s-v1`. The recogniser sits behind an interface (ADR 0003); replacing it is
   bounded work, not an architectural change.

   **If asked "so does it work?"** — the platform works: federation, evidence chain,
   audit ledger, journey reconstruction and alerting all run end to end on live
   government feeds. The recogniser is the component that needs work, and we can tell
   you exactly how much because we measured it.

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
   recovered on 2026-08-27: 25 of 30 cameras deliver frames, cameras 17 and 18 return
   HTTP 500, three time out. `docs/SUPPORT_QUERY.md` is the fault report we prepared.

5. **There is no hosted URL unless one has been deployed since.** The container stack
   is verified against nine checks; the deployment needs the team's platform account.
   `docs/DEPLOYMENT.md` §4.

6. **Face recognition is not built.** Deliberately. It stays unbuilt until all four
   governance controls fit — off by default per camera, recorded authorisation with a
   named officer and expiry, gallery scoped to an authorised case, separately
   auditable. An ungoverned biometric feature is worse than none.
