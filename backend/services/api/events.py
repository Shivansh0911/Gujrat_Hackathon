"""In-process event bus.

The interface is deliberately Kafka-shaped -- topics, ordered delivery, explicit
subscription -- so that swapping the backend at statewide scale is a deployment
change rather than a rewrite of every producer. See ADR 0001.

Producers must never assume a subscriber ran: no shared mutable state, no synchronous
dependency on a handler's side effect. Those constraints are what a Kafka migration
actually requires, and enforcing them now is the point of having the abstraction
before we have the broker.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from enum import StrEnum
from typing import Any, Callable

log = logging.getLogger(__name__)

Handler = Callable[[str, dict[str, Any]], None]


class CameraEvent(StrEnum):
    ADDED = "camera.added"
    REMOVED = "camera.removed"
    PROPERTIES_CHANGED = "camera.properties_changed"
    HEALTH_CHANGED = "camera.health_changed"


class DetectionEvent(StrEnum):
    PLATE_READ = "detection.plate_read"


class AlertEvent(StrEnum):
    RAISED = "alert.raised"
    UPDATED = "alert.updated"


class EventBus:
    """Synchronous in-process fan-out."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subscribers[str(topic)].append(handler)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        topic = str(topic)
        for handler in list(self._subscribers.get(topic, ())):
            try:
                handler(topic, payload)
            except Exception:
                # One failing subscriber must not prevent the others from seeing the
                # event, and must never fail the producer's transaction. Logged with
                # a stack trace rather than swallowed.
                log.exception("event handler failed for topic=%s", topic)

    def clear(self) -> None:
        """Test helper: drop all subscriptions."""
        self._subscribers.clear()


event_bus = EventBus()
