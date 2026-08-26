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

**Type plate:** `KA25AB144`
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

**Then demonstrate the negative case.** Type `MH99ZZ9999`, same purpose, trace. The
result distinguishes *plate never seen anywhere* from *seen, but not in this window*
— different findings, and an investigator needs to tell them apart.

### 2.3 Alert Desk — 60 seconds

**8 alerts**, all grouped as movement alerts.

- **Click the `KA25AI11G` card** — this is the one to demonstrate. It is a
  **`fuzzy_1`** match: the detection read `KA25AI116`, the watchlist holds
  `KA25AI11G`, and `G`/`6` are a known OCR confusion pair. **An exact-match system
  finds nothing here.** The card shows the match type and the reduced score.
- Note the three timestamps on each card: `observed_at_utc`, stream `PTS`, and the
  read confidence. PTS is what makes a detection reproducible from the original
  stream rather than merely displayed.
- **Acknowledge** it, then **Resolve → False positive**. Explain that the disposition
  feeds the per-camera false-positive rate on the Health screen — the platform
  measuring its own precision rather than asserting it.
- **Discrimination matters:** the watchlist holds 9 entries. Five are valid
  registrations from Maharashtra, Delhi, Rajasthan, Tamil Nadu and Uttar Pradesh that
  are **correctly ignored**. A demo where everything matches is not believable.

### 2.4 Health — 40 seconds

- Sort by **fps drift**. Declared versus measured frame rate side by side.
  **Say this:** *"The integration guide warns that a camera's reported frame rate
  cannot be trusted. On this estate, 20 of 30 cameras declare no rate at all. Every
  measured figure here comes from stream presentation timestamps."*
- Click **Fault report** on any camera. It generates the organiser's §2.5 payload
  verbatim — camera id, exact URL, client and version, UTC timestamp, client-side log
  — ready to send.

### 2.5 Audit — 20 seconds

In the API docs (`/docs`), call **`GET /audit/verify`**. It recomputes the whole hash
chain and returns validity. Every journey query, every pin drop, every alert
acknowledgement and every failed login is in there, each entry committing to the one
before it.

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

If the gateway **does** recover, run `python scripts/probe_catalogue.py --sequential
--emit-evidence` to capture live properties, then extend ingest across the catalogue.

---

## 4. Honest limitations — state these before a judge finds them

State these up front. They are the difference between a jury trusting the rest of the
demonstration and not.

1. **The own-feed clip is third-party.** A CC BY 3.0 Wikimedia clip of the
   Hubli–Dharwad BRTS route, attributed in `data/own_feed/SOURCE.md`. Being Karnataka
   footage, the plates read `KA…`/`KL…` rather than `GJ…`.

2. **The four `REPLAY-…` cameras are a replay harness, not live feeds.** The
   government multi-camera feed is unavailable, so route reconstruction is
   demonstrated by running the full ANPR pipeline separately against four registry
   positions. Every detection is a genuine inference result with a real crop —
   nothing is copied between cameras and no plate is invented. What is simulated is
   *which camera saw it*. The `REPLAY` prefix is visible in the UI for exactly this
   reason.

3. **The OCR is imperfect, and the evidence crop shows it.** On hop 1 the crop reads
   `KA-25 AB-1542` while the system recorded `KA25AB144`. That is a genuine misread,
   and it is visible precisely because we show the crop. Do not hide this — it is the
   strongest argument for why evidence images belong in the interface at all. Recall
   and precision have not yet been measured against annotated ground truth.

4. **Multi-frame fusion is barely exercised on this footage.** The clip is shot from a
   moving bus, so the scene-cut detector fires often and tracks reset; most detections
   fused a single frame. The logic is proven by unit tests, not by this clip. Fixed
   CCTV is where it will show properly.
