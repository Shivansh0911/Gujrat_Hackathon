# ADR 0001 — Domain events go through an interface, not through Kafka

- **Status:** Accepted
- **Date:** 2026-08-25
- **Deciders:** SETU engineering
- **Supersedes:** the topology in the master build prompt §4, which names Kafka directly

## Context

The reference architecture places Kafka at the centre of the platform, carrying
`frames.meta`, `detections.raw`, `detections.anpr`, `tracks.local`, `alerts`,
`camera.health` and `audit.chain`. At the statewide target of ~80,000 cameras that is
the right answer: durable partitioned logs, independent consumer groups, replay after a
consumer outage, and backpressure that does not lose evidence.

The system we are building and demonstrating runs against **30 cameras**. A jury will
ask why the HLD shows Kafka and the repository does not, and the answer has to be
better than "we ran out of time".

## Decision

Domain events are published through an `EventBus` interface whose shape is deliberately
Kafka's: topics, keys, ordered delivery per key, consumer groups, explicit offset
acknowledgement, and at-least-once semantics with idempotent consumers.

Two implementations exist behind it:

- `InProcessEventBus` — the default for the demonstration stack. Backed by an asyncio
  queue per topic with the same ordering and acknowledgement contract.
- `KafkaEventBus` — the statewide deployment target, selected by configuration.

Choosing between them is a deployment decision (`SETU_EVENT_BUS=inprocess|kafka`), not a
code change in any producer or consumer.

## Rationale

**Kafka at 30 cameras is unjustified operational weight.** A broker, its coordinator,
topic provisioning, consumer-group rebalancing and retention tuning are real operational
surface. On a single demonstration machine with a 6 GB GPU that surface competes for
memory with the analytics that actually score points, and every minute spent debugging a
rebalance is a minute not spent on route reconstruction.

**The interface is the architecture; the broker is a deployment choice.** What makes the
platform scale is that producers never assume in-process delivery — no shared mutable
state, no synchronous call into a consumer, no reliance on a handler having already run.
Those constraints are enforced by the interface and are what a Kafka migration actually
requires. Writing them against an in-process backend proves the discipline; writing them
against Kafka proves nothing extra.

**It keeps our scaling claim honest.** Claiming "Kafka-based, therefore scalable" while
running 30 cameras on one box is the kind of assertion a technical jury tests by asking
what happens when a consumer dies. Being able to answer "the offset is not acknowledged,
the event is redelivered, and here is the test that proves it" is stronger than a broker
in a diagram.

## Consequences

**Accepted costs**

- The in-process bus does not survive a process restart. Events in flight at shutdown
  are lost. This is acceptable for `frames.meta` and `camera.health`, which are
  regenerated continuously, and is **not** acceptable for `audit.chain` — audit entries
  are therefore written transactionally to Postgres and published to the bus as a
  notification, never *only* published. Losing a notification cannot lose an audit entry.
- Multi-process deployment requires the Kafka backend. The demonstration stack is
  single-process by design.

**Obligations this creates**

- Both implementations are verified by the same contract test suite. A backend is not
  "supported" until it passes the shared suite covering ordering per key, redelivery on
  negative acknowledgement, and consumer-group fan-out.
- No producer or consumer may import a concrete bus. CI enforces this.
- Event payloads are versioned and serialised as JSON with an explicit schema from the
  first commit, because retrofitting a schema onto an in-process bus that never needed
  one is how the Kafka migration turns into a rewrite.

## Alternatives considered

**Run Kafka anyway.** Honest to the HLD, but spends operational budget and demo-machine
memory on a capability that 30 cameras cannot exercise. A broker sitting idle is not
evidence of scale.

**Redis Streams as a middle ground.** Redis is already in the stack, and Streams offer
consumer groups and acknowledgement. Rejected as the *default* because it is a third
implementation to maintain and its delivery semantics differ from Kafka's in ways
(trimming, no compaction) that would let a producer drift into assumptions Kafka does
not honour. It remains a viable backend behind the same interface if a multi-process
demo is needed before Kafka lands.

**Direct function calls, deferring events entirely.** Fastest to write and the most
expensive to undo: it permits synchronous coupling everywhere, and the migration cost
then falls due exactly when the system is under scale pressure.
