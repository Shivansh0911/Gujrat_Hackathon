"""JSON log lines carrying the correlation id, with redaction still in front of them.

Why JSON
--------
The existing format is readable by a person at a terminal and almost useless to a log
aggregator: `"%(asctime)sZ %(levelname)-7s %(name)s: %(message)s"` has to be parsed with
a regex that breaks the first time a message contains a colon. A government ops team
ships logs to something that indexes fields, and "find every request with correlation id
a1b2c3" should be a field query rather than a grep.

Both formats are kept. A developer running the stack locally wants the compact human
line; a deployment wants JSON. `SETU_LOG_FORMAT=json` selects it, and the entrypoint sets
that in the container.

Redaction stays in front
------------------------
`redact.py` already strips secrets from every record, and this must not be a way around
it. The filter is applied to the handler exactly as before, and the formatter runs
*after* it -- so a structured field containing an access code is redacted by the same
code path that protects the human-readable line. Adding a second serialisation route
without the filter would be a quiet regression in the one property that matters here.

The correlation id
------------------
A `ContextVar` rather than a parameter. Threading a correlation id through every
function that might log is the kind of change that gets half-done, and the half that was
missed is the half where the interesting failure happens. The API middleware sets it per
request; anything logging during that request picks it up without knowing it exists.
"""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

#: Environment variable selecting the format. Anything other than "json" keeps the
#: human-readable line, so a developer's terminal is unaffected by default.
FORMAT_ENV = "SETU_LOG_FORMAT"

#: Set per request by the API middleware; read by the formatter. Default empty rather
#: than None so a line logged outside a request has the field with an empty value,
#: which an aggregator handles better than a missing key.
correlation_id: ContextVar[str] = ContextVar("setu_correlation_id", default="")

#: Fields the stdlib puts on every record. Everything else a caller attached through
#: `extra=` is emitted as a top-level field, which is the point of using `extra` at all.
_STANDARD = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation id and any `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # RFC 3339 with an explicit offset. A log line whose timezone has to be
            # inferred from the host is one that cannot be correlated across machines.
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        current = correlation_id.get()
        if current:
            payload["correlation_id"] = current

        if record.exc_info:
            # Already redacted by the filter, which rewrites exc_text before this runs.
            payload["exception"] = record.exc_text or self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                # A field that will not serialise becomes its repr rather than taking
                # the whole line down. A log formatter that can raise is one that loses
                # the message explaining why everything else broke.
                payload[key] = repr(value)

        return json.dumps(payload, default=str)


def install(level: int = logging.INFO, force_json: bool | None = None) -> None:
    """Configure logging, with redaction attached to every handler.

    Delegates to `redact.install` so the redacting filter is applied exactly as before,
    then swaps the formatter when JSON is selected. The order matters: the filter
    rewrites `record.msg` and `record.exc_text`, and the formatter reads them -- so
    redaction happens first whichever format is in use.
    """
    from services.common import redact

    redact.install(level=level)

    use_json = (
        force_json
        if force_json is not None
        else os.environ.get(FORMAT_ENV, "").strip().lower() == "json"
    )
    if not use_json:
        return

    formatter = JsonFormatter()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def bind(value: str) -> None:
    """Attach a correlation id to everything logged on this task."""
    correlation_id.set(value)


def current() -> str:
    return correlation_id.get()


__all__ = ["FORMAT_ENV", "JsonFormatter", "bind", "correlation_id", "current", "install"]
