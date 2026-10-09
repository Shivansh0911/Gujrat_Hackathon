"""Prometheus-format metrics, written by hand rather than with a client library.

Why no client library
---------------------
`prometheus_client` is Apache-2.0 and would be licence-clean. It is not here because
what this endpoint needs is a text rendering of counters that already exist elsewhere --
camera health counts, alert volumes, delivery backlogs -- most of which live in Postgres
rather than in process memory. A client library's value is its registry and its
multiprocess handling; with one worker and the real numbers in the database, that is
machinery without a job.

The exposition format is four lines of string formatting and is specified precisely, so
writing it is cheaper than owning the dependency. If this grows histograms with
per-request observation, that trade reverses and the library should be adopted.

What is measured, and the one thing that is not
------------------------------------------------
Counters that answer an operational question: is ingest running, are alerts being
raised, is the webhook queue draining, how many cameras are unreachable.

Inference latency percentiles are **not** here. They would need per-request observation
in process memory, which is the one thing a single-worker free-tier deployment restarts
often enough to make meaningless -- a p99 computed since the last cold start is a number
with the shape of a measurement and the content of an anecdote. The ANPR benchmark
reports latency properly, against a fixed clip, and that is where it belongs.

Cardinality
-----------
No label carries a camera id, a plate or a key id. A metric labelled by camera on an
80,000-camera estate is 80,000 time series, which is how a monitoring system falls over
while reporting that everything is fine.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_STARTED = time.time()

#: Prefix on every metric, as the convention requires.
NS = "setu"


@dataclass(frozen=True)
class Metric:
    name: str
    kind: str  # counter | gauge
    help_text: str
    value: float
    labels: dict[str, str] | None = None

    def render(self) -> str:
        label_part = ""
        if self.labels:
            inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(self.labels.items()))
            label_part = f"{{{inner}}}"
        # Integers render without a decimal point; Prometheus accepts both, but an
        # integer-looking counter is easier to read in an alert rule.
        rendered = int(self.value) if float(self.value).is_integer() else self.value
        return f"{self.name}{label_part} {rendered}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


#: Each query returns a single number. Written out rather than generated, because a
#: metric whose meaning is assembled from a loop is one nobody can explain in an
#: incident.
_QUERIES: tuple[tuple[str, str, str, str], ...] = (
    (
        f"{NS}_cameras_total",
        "gauge",
        "Cameras in the registry, excluding decommissioned.",
        "SELECT count(*) FROM camera WHERE status <> 'DECOMMISSIONED'",
    ),
    (
        f"{NS}_cameras_unreachable",
        "gauge",
        "Cameras whose last probe failed.",
        "SELECT count(*) FROM camera WHERE status = 'UNREACHABLE'",
    ),
    (
        f"{NS}_cameras_placed",
        "gauge",
        "Cameras with a coordinate. The rest cannot contribute a journey hop.",
        "SELECT count(*) FROM camera WHERE geom IS NOT NULL",
    ),
    (
        f"{NS}_detections_total",
        "counter",
        "Plate reads recorded.",
        "SELECT count(*) FROM detection",
    ),
    (
        f"{NS}_detections_last_hour",
        "gauge",
        "Plate reads in the last hour. Zero on a live estate means ingest has stopped.",
        "SELECT count(*) FROM detection WHERE observed_at_utc > now() - interval '1 hour'",
    ),
    (
        f"{NS}_sightings_total",
        "counter",
        "Vehicle sightings recorded, with or without a readable plate.",
        "SELECT count(*) FROM vehicle_sighting",
    ),
    (
        f"{NS}_sightings_last_hour",
        "gauge",
        "Vehicle sightings in the last hour.",
        "SELECT count(*) FROM vehicle_sighting WHERE observed_at_utc > now() - interval '1 hour'",
    ),
    (
        f"{NS}_sightings_without_fingerprint",
        "gauge",
        "Sightings whose crop failed the quality gate. Still useful for counting and "
        "dwell; not searchable by appearance.",
        "SELECT count(*) FROM vehicle_sighting WHERE appearance_vector IS NULL",
    ),
    (
        f"{NS}_alerts_open",
        "gauge",
        "Alerts not yet resolved.",
        "SELECT count(*) FROM alert WHERE state <> 'RESOLVED'",
    ),
    (
        f"{NS}_alerts_last_hour",
        "gauge",
        "Alerts raised in the last hour.",
        "SELECT count(*) FROM alert WHERE observed_at_utc > now() - interval '1 hour'",
    ),
    (
        f"{NS}_watchlist_active",
        "gauge",
        "Watchlist entries currently in force.",
        "SELECT count(*) FROM watchlist_entry WHERE active"
        " AND valid_from <= now() AND valid_to > now()",
    ),
    (
        f"{NS}_audit_entries_total",
        "counter",
        "Entries in the hash-chained ledger.",
        "SELECT count(*) FROM audit_entry",
    ),
    (
        f"{NS}_webhook_deliveries_pending",
        "gauge",
        "Deliveries awaiting a first attempt or a retry. A number that only grows "
        "means the dispatcher is not running.",
        "SELECT count(*) FROM webhook_delivery WHERE state IN ('pending','failed')",
    ),
    (
        f"{NS}_webhook_deliveries_dead",
        "gauge",
        "Deliveries that exhausted their retries and are awaiting manual replay.",
        "SELECT count(*) FROM webhook_delivery WHERE state = 'dead'",
    ),
    (
        f"{NS}_webhook_subscriptions_disabled",
        "gauge",
        "Subscriptions switched off after repeated failures.",
        "SELECT count(*) FROM webhook_subscription WHERE disabled_at IS NOT NULL",
    ),
    (
        f"{NS}_api_keys_active",
        "gauge",
        "Service-account keys neither revoked nor expired.",
        "SELECT count(*) FROM api_key WHERE revoked_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > now())",
    ),
)


def collect(session: Session) -> list[Metric]:
    """Gather every metric. A failing query yields nothing rather than failing the scrape.

    A metrics endpoint that 500s during an incident removes the monitoring exactly when
    it is needed. A missing series is visible in a dashboard as a gap; a failed scrape
    is visible as "monitoring is broken", which sends an operator chasing the wrong
    thing.
    """
    metrics: list[Metric] = [
        Metric(
            f"{NS}_process_uptime_seconds",
            "gauge",
            "Seconds since this process started.",
            time.time() - _STARTED,
        )
    ]

    for name, kind, help_text, query in _QUERIES:
        try:
            value = session.execute(text(query)).scalar()
        except Exception:  # noqa: BLE001 -- see the docstring
            log.warning("metric %s could not be collected", name, exc_info=True)
            continue
        metrics.append(Metric(name, kind, help_text, float(value or 0)))

    # Camera health by status, as one series per status rather than one per camera.
    # Bounded cardinality by construction: the enum has eight members.
    try:
        rows = session.execute(
            text(
                "SELECT status, count(*) FROM camera"
                " WHERE status <> 'DECOMMISSIONED' GROUP BY status"
            )
        ).all()
        for status, count in rows:
            metrics.append(
                Metric(
                    f"{NS}_cameras_by_status",
                    "gauge",
                    "Cameras by lifecycle status.",
                    float(count),
                    labels={"status": str(status)},
                )
            )
    except Exception:  # noqa: BLE001
        log.warning("camera status metric could not be collected", exc_info=True)

    return metrics


def render(metrics: list[Metric]) -> str:
    """The Prometheus text exposition format.

    HELP and TYPE are emitted once per metric name even when a metric has several
    label sets, which is what the format requires -- repeating them is a parse error in
    some scrapers and merely noisy in the rest.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for metric in metrics:
        if metric.name not in seen:
            seen.add(metric.name)
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
        lines.append(metric.render())
    # A trailing newline is required by the format.
    return "\n".join(lines) + "\n"


__all__: list[str] = ["NS", "Metric", "collect", "render"]
