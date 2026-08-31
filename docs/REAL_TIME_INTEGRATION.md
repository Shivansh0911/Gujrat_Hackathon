# Going live: integrating a real camera network

This document answers one question directly, for a reader deciding whether SETU is a
hackathon artefact or the beginning of a deployable system: **what would actually have
to happen to point this at Gujarat's real camera estate, and what would break?**

The short answer is that the extension point already exists and has two independent
implementations exercised against real infrastructure. What is missing is not
architecture. It is departmental access, and hardware.

Everything below is either code in this repository or a measurement recorded in it.
Where something is unproven, it says so.

---

## 1. The one thing a new camera network has to implement

Every camera in SETU reaches the platform through a single Python protocol
(`services/ingest/source.py`):

```python
class CameraSource(Protocol):
    def probe(self) -> CameraCapabilities: ...
    def open(self) -> Iterator[Frame]: ...
    def health(self) -> HealthReport: ...
    def observed_at(self, frame: Frame) -> datetime: ...
    @property
    def clock_confidence(self) -> float: ...
    def close(self) -> None: ...
```

Nothing above this line — not the analytics, not the watchlist matcher, not the
journey reconstruction, not the console — knows how a frame arrived. That claim is not
architectural optimism; it is demonstrated by the fact that **three** implementations
already exist and are interchangeable:

| Implementation | Source | Status |
|---|---|---|
| `GatewaySource` | The challenge gateway, HLS with RTSP preferred where reachable | Exercised against the live estate; 8/8 on the organiser's feed contract |
| `FileSource` | Recorded footage | Produces every own-feed detection in the deployed instance |
| `Deadlined` | A wrapper enforcing a wall-clock budget on any of the above | Used by the gateway ingest runner |

Adding ONVIF, a vendor SDK (Hikvision, Dahua, Axis), or direct RTSP to a departmental
NVR is **a new class implementing six methods**. It is not a change to anything else.

### 1.1 What an ONVIF adapter would actually involve

Sketching it honestly, because "just implement the interface" hides the real work:

| Method | What it needs | Difficulty |
|---|---|---|
| `probe()` | ONVIF `GetProfiles` → stream URI, codec, resolution | Straightforward; the WS-Discovery/SOAP layer is a solved library problem |
| `open()` | RTSP from the profile's `GetStreamUri`, then the **existing** `StreamSession` | Nearly free — the transport, backoff and reconnect logic is already written and contract-tested |
| `observed_at()` / `clock_confidence` | ONVIF cameras often expose a real device clock via `GetSystemDateAndTime` | **Better than what we have today.** A device clock is a stronger anchor than the join-time estimate `GatewaySource` is forced into, so `clock_confidence` would rise, not fall |
| `health()` | `GetServiceCapabilities` plus the frame-level signals already collected | Straightforward |

The one genuinely new piece is credential management: ONVIF is authenticated per
camera, so a secret store keyed by camera becomes necessary. That is a deployment
concern with standard answers, and it is called out in §5 rather than waved away.

---

## 2. What does not change, and why that matters

These are already built and tested, and a real camera network does not disturb any of
them. This is the actual argument for the design.

- **Timing.** Every observation time derives from presentation timestamps, never from
  arrival time and never from a declared frame rate. A CI job asserts the codebase
  contains exactly two reference-only reads of `CAP_PROP_FPS` and fails the build on a
  third. Real cameras lie about their frame rate — 12 of 16 on this estate diverge,
  one by +87% — so this is the property most likely to be quietly violated during an
  integration, and it is the one guarded mechanically.
- **Capability probing.** The catalogue is treated as a claim, never as truth: 19 of 30
  cameras here declare no codec or resolution at all, and the `live` flag turned out
  not to be a health signal. Capabilities are probed. A new network changes the
  numbers, not the policy.
- **Tenant isolation.** Row-level security is enforced in PostgreSQL, not in the
  application: nine tests issue raw SQL that bypasses the application entirely, and
  `setu_app` is verified `rolsuper=false, rolbypassrls=false` on the deployed database.
  Adding a department is a row and a role assignment.
- **The audit ledger.** Hash-chained and append-only at the database level — the
  application holds no `UPDATE` grant on it. Every plate search records its stated
  purpose *before* the search runs, so a search returning nothing is recorded exactly
  like one returning a route.
- **Evidence.** Ed25519 detached signatures over canonical JSON manifests, verifiable
  by any Ed25519 implementation without SETU.

---

## 3. Scaling from 30 cameras to 80,000

The honest version of this, including the number that got worse when measured.

Throughput was measured on a single CPU worker with no GPU. At the resolution these
cameras publish (2560×1440), **one worker sustains 0.81 cameras** — which extrapolates
to roughly **98,500 workers** for an 80,000-camera estate processed centrally.

A lower-resolution sub-stream raises that to 3.82 cameras per worker at 704×396, and it
is tempting to quote. We do not, because accuracy collapses before the saving is
realised: the same footage yields 8 grammar-valid plates at 2560×1440, 2 at 1280×720,
and **none at all** at 704×396. That figure is throughput at an operating point where
the recogniser reads nothing.

The defensible conclusion is the full-resolution one, and it is the stronger argument
anyway: **centralised processing is not affordable at the resolution ANPR actually
requires.** Hence the design.

### 3.1 The scaling path, in order of what each step buys

1. **Edge inference — the decisive step.** Each site processes its own cameras at full
   resolution and emits detection records of a few hundred bytes instead of a video
   stream. What makes an edge node affordable is already measured: PTS-interval
   sampling to 5 analytic fps removes 83.2% of frames, and the motion gate removes
   32.7% of what survives — **86.3% of decoded frames never reach the detector**, with
   wall time down 54.7% and *identical* output (22 plate regions, 2 valid
   registrations either way). It is a free saving, not a quality trade.
2. **GPU where camera density demands it.** Every figure above is CPU-only. A GPU
   changes the worker count, not the architecture.
3. **Durable event log.** Domain events already flow through an interface whose shape
   is Kafka's, with an in-process backend at this scale. Swapping the backend is a
   deployment choice, not a rewrite — see [`adr/0001-event-bus-abstraction.md`](adr/0001-event-bus-abstraction.md).
4. **Time-partitioned storage.** `detection` is already a TimescaleDB hypertable
   partitioned on `observed_at_utc`, the column every time-window query groups by.
   Journey queries currently run at median 38 ms, p95 60 ms against a 12-hour window.

### 3.2 The highest-value work outstanding

**Establishing the lowest resolution that preserves plate legibility.** Every scaling
number above is bounded by it, and it is a measurement nobody has made — not an
engineering task. It would need a resolution sweep against annotated ground truth,
which is roughly a day of work and would change the hardware budget by an order of
magnitude in either direction.

Independent footage supports the same conclusion. A second Creative Commons clip — a
real, traffic-filled junction in Cuttack, Odisha at 1080×606 from an elevated position —
ran through the unmodified pipeline and produced **zero** plate boxes across 2,250
frames. Camera placement and resolution bound this problem far more tightly than model
choice does. See Finding 16 in [`DISCOVERY.md`](DISCOVERY.md).

---

## 4. Network reality: this is the part people underestimate

Integrating a government camera estate is mostly a network problem, and this codebase
has already been bruised by it once.

- **Ports will be blocked.** RTSP:8554 and WHEP:8889 are unreachable on the challenge
  gateway because Cloudflare proxies 443/80 only. The transport layer already selects
  HLS when RTSP is unreachable and forces TCP when it is — [`adr/0002-hls-transport.md`](adr/0002-hls-transport.md).
- **Streams will be behind quirks.** This gateway gates HLS on a `cookieCheck`
  parameter that FFmpeg drops, so the master playlist is resolved by us and the variant
  handed to FFmpeg. Expect one of these per vendor.
- **Camera URLs are attacker-influenced input.** A camera record contains a URL, and
  anything that fetches a URL supplied by a database row is an SSRF vector. The guard
  (`services/common/ssrf.py`) enforces a scheme allowlist (`http`, `https`, `rtsp`,
  `rtsps`), a port allowlist, blocks private and link-local address ranges, and
  **re-verifies DNS immediately before connecting** so a rebinding attack cannot slip
  between validation and use.
- **Upstream will be down, and the platform must say so rather than look broken.** A
  passive watcher polls the catalogue every 60 s and records the transition, so the
  console can answer "when did it stop", not just "is it up". Reachability is
  three-valued: `null` means not yet checked, and the card says so rather than
  presenting an unknown as an outage.

That last point is not hypothetical. The challenge gateway has returned a Cloudflare
502 on every endpoint since 31 August 2026, and the platform's handling of that is
visible on the Health page.

---

## 5. What SETU needs from departments

Not code. This is the actual critical path, and none of it is engineering.

| Need | Why | Who provides it |
|---|---|---|
| **Stream endpoints and credentials** | One adapter per VMS family; credentials per camera for ONVIF | Each department's IT |
| **A camera register with positions** | 26 of 34 cameras here have no surveyed coordinate; the Coverage page quantifies exactly what that costs | Departments, via the CSV import already built |
| **Departmental demarcation** | The tenancy model and RLS are built and tested, but the challenge catalogue carries no department field, so all 34 cameras sit in one tenant. **This is a data gap, not a code gap** — verified by temporarily moving two cameras to a second department, confirming the filter worked, and reverting | Departments |
| **A retention policy** | Evidence crops and detections need a lawful retention period; the schema supports it, the policy is not ours to set | Legal / policy |
| **Edge hardware at sites** | §3 — the architecture assumes it | Procurement |

---

## 6. What we would not claim works yet

Stated plainly, because a roadmap that lists only successes is not a roadmap.

- **No adapter other than HLS/RTSP-over-gateway and file has been written.** ONVIF is
  designed, not built. The interface makes it small; "small" is not "done".
- **Speed-based flagging is not built.** It needs genuine cross-camera timestamps, and
  the only multi-camera data available is a replay harness where camera attribution is
  simulated. A speed alert computed from simulated attribution would be a fabricated
  capability presented as a real one.
- **Edge deployment is measured, not deployed.** The compute reductions are real and
  benchmarked; no edge node has actually run in the field.
- **ANPR accuracy is 29.6%** precision and recall on this footage, and the ceiling is
  partly the estate's: 9,158 frames across 25 live cameras yielded three
  human-legible plates. Better cameras would move this more than a better model would.

---

## 7. Summary for someone deciding

The parts that are hard to retrofit — timing discipline, tenant isolation enforced in
the database, a tamper-evident ledger, evidence that verifies without the system that
produced it, an ingest abstraction with three working implementations — are built and
tested now. The parts that remain are access, hardware, and one measurement.

That is the right way round. A system that federates 80,000 cameras across 26
departments fails on trust and on network reality long before it fails on features, and
those are precisely the parts that cannot be bolted on afterwards.
