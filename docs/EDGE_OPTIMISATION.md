# Edge processing and compute optimisation — measured

**Measured 2026-08-28** on `data/own_feed/commons_traffic_clip.mp4` (2560×1440, H.264,
90 s, 29.97 fps measured from PTS), CPU only, no GPU. Reproduce with the commands in
§5. Every figure below is from those runs; nothing here is estimated.

---

## The claim, stated precisely

SETU does not send video to a datacentre to decide whether it is interesting. Two
independent reductions run before the expensive plate detector sees anything, and
together they keep **86.3% of decoded frames away from it**.

That number has been quoted before as "the motion gate removes 86% of frames". **That
attribution is wrong, and this document corrects it.** Re-measuring to write this up
separated the two effects for the first time, and the motion gate is the smaller of
them.

---

## 1. Where the reduction actually comes from

Four configurations of the same pipeline over the same 901 frames:

| Analytic rate | Motion gate | Frames reaching the detector | Wall time |
|---|---|---:|---:|
| 30 fps (no sampling) | off | 901 — 100% | 41.9 s |
| 30 fps (no sampling) | on | 606 — 67.3% | 36.6 s |
| 5 fps | off | 151 — 16.8% | 19.8 s |
| **5 fps (production)** | **on** | **123 — 13.7%** | **19.0 s** |

Reading the table:

- **PTS-based analytic sampling is the dominant win.** Holding the gate off, moving
  from 30 fps to 5 fps takes 901 frames down to 151 — an 83.2% reduction, and 41.9 s
  to 19.8 s of wall time. This is most of the 86%.
- **The motion gate is a real but smaller second win.** Holding sampling off, it takes
  901 frames to 606 — 32.7% removed, 41.9 s to 36.6 s.
- **Together**, 901 → 123, and 41.9 s → 19.0 s: **86.3% of frames never reach the
  detector, and wall time falls by 54.7%.**

## 2. The gate costs nothing in output

At the production operating point, enabling the motion gate changed the pipeline's
output not at all:

| | Gate off | Gate on |
|---|---:|---:|
| Plate regions detected | 22 | 22 |
| Grammar-valid registrations | 2 | 2 |

Same reads, 28 fewer detector invocations. That is the property that makes the gate
safe to run: it discards frames in which nothing moved, and a stationary scene has no
vehicle arriving to miss.

## 3. Why sampling is on PTS and not on frame count

The analytic rate is expressed as a minimum interval between *presentation
timestamps*, never as "every sixth frame". Frame cadence on this estate is not
uniform — 12 of 16 catalogued cameras deliver a frame rate that differs from the one
they declare, one by +87% — so "every sixth frame" means a different real-world
interval on every camera, and a different one on the same camera after a reconnect.
An interval on PTS means 5 fps of *scene*, whatever the feed is doing.

This is the same rule as the rest of the platform: timing comes from PTS, never from a
declared rate and never from arrival time.

## 4. What this buys at estate scale

At 2560×1440 one CPU worker sustains 0.81 cameras with both reductions active. Without
them the same worker would sustain roughly 0.37 — the 54.7% wall-time saving expressed
the other way round. Across 80,000 cameras that is the difference between order
98,500 workers and order 216,000.

Neither number is an argument for centralising. Both are an argument against it, and
that is the point: the honest conclusion from these measurements is that a centralised
design is implausible at this resolution, which is why the architecture moves
**metadata to the centre and leaves video at the edge**. The two reductions documented
here are what make an edge node affordable — they are the reason a modest box at a
junction can keep up with its own cameras.

**For a low-connectivity site**, the consequence is the same one seen from the other
end: what leaves the site is a detection record of a few hundred bytes, not a video
stream. A site on a metered or intermittent link stays useful, because the link only
carries what was found.

## 5. Reproducing this

```bash
cd backend
# Production configuration
python scripts/run_anpr.py ../data/own_feed/commons_traffic_clip.mp4 --max-frames 901

# Isolate the two effects
python scripts/run_anpr.py ../data/own_feed/commons_traffic_clip.mp4 --max-frames 901 \
    --analytic-fps 30                      # gate only
python scripts/run_anpr.py ../data/own_feed/commons_traffic_clip.mp4 --max-frames 901 \
    --analytic-fps 30 --motion-threshold 0 # neither
python scripts/run_anpr.py ../data/own_feed/commons_traffic_clip.mp4 --max-frames 901 \
    --motion-threshold 0                   # sampling only
```

`--motion-threshold 0` makes the gate's `score >= threshold` test always true, which is
how "gate off" is measured; there is no separate disable flag.

## 6. What is not claimed

- **This is not an edge deployment.** The reductions are real and measured, and they
  are what makes edge processing viable, but SETU has not been deployed to an edge
  node. The architecture is documented; the deployment is not done.
- **No bandwidth figure is quoted**, because none has been measured. The bandwidth
  argument follows from moving metadata instead of video, and it is stated as an
  architectural consequence rather than a number.
- **The gate is fixed at 2.5, not adaptive.** Lowering the analytic rate under load
  and restoring it when idle would make this genuinely load-adaptive. It is not built.
- **These figures are from one clip on one machine.** A scene with constant motion —
  a busy junction at rush hour rather than this one — would pass more frames and save
  less. The gate's benefit is scene-dependent by construction.

---

Related: [`HLD_RECONCILIATION.md`](HLD_RECONCILIATION.md) for the scalability claim
these numbers feed, and [`DISCOVERY.md`](DISCOVERY.md) for the declared-versus-measured
frame rate finding that forces PTS-based sampling.
