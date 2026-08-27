"""Credential redaction for logs.

§6 requires that upstream URLs and credentials never leave the adapter boundary.
Developers will nonetheless log a stream handle during debugging at 2am the night
before evaluation. This filter is the backstop: it is installed on the root logger
at process start, so a credential cannot reach a log sink even if someone formats
one into a message.

Redaction happens on the *formatted* message and on args, and is deliberately
aggressive -- a false positive costs a debugging session, a false negative puts a
government camera credential in a log file that ends up in the submission bundle.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # userinfo in any URL: scheme://user:pass@host -> scheme://[REDACTED]@host
    re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@", re.IGNORECASE),
    # Bare stream URLs. We redact the whole URL, not just credentials: an upstream
    # RTSP path is itself sensitive infrastructure detail under §6.
    re.compile(r"\brtsps?://\S+", re.IGNORECASE),
    # key=value / "key": "value" for known credential-ish names.
    re.compile(
        r"(?P<key>\b(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|"
        r"bearer|credential|private[_-]?key|conn(?:ection)?[_-]?string)\b)"
        # The optional leading quote matters: in JSON the key's own closing quote
        # sits between the key and the colon, and without it every logged JSON
        # credential passes through unredacted.
        r"(?P<sep>\"?\s*[:=]\s*\"?)(?P<val>[^\s,;\"'}]+)",
        re.IGNORECASE,
    ),
)


def redact(text: str) -> str:
    """Return `text` with credentials and upstream stream URLs masked."""
    out = _PATTERNS[0].sub(lambda m: f"{m.group('scheme')}{_REDACTED}@", text)
    out = _PATTERNS[1].sub(_REDACTED, out)
    out = _PATTERNS[2].sub(lambda m: f"{m.group('key')}{m.group('sep')}{_REDACTED}", out)
    return out


class RedactingFilter(logging.Filter):
    """Scrubs every record before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render args in first so an interpolated credential is caught too, then
            # clear args to avoid double-formatting the already-rendered message.
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - malformed format string
            # Never let redaction failure swallow a log line; fall back to the raw
            # template, which cannot contain interpolated secrets.
            rendered = str(record.msg)
        record.msg = redact(rendered)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(level: int = logging.INFO, **kwargs: Any) -> None:
    """Configure root logging with redaction attached to every handler."""
    logging.basicConfig(
        level=level,
        format="%(asctime)sZ %(levelname)-7s %(name)s: %(message)s",
        **kwargs,
    )
    filt = RedactingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        # Filter on the handler, not the logger: logger-level filters are skipped
        # for records propagated up from child loggers.
        handler.addFilter(filt)
