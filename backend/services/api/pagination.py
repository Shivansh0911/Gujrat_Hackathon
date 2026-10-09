"""Cursor pagination, because offset is wrong on a table that is still being written.

Why not offset
--------------
`LIMIT 50 OFFSET 500` has two problems here, and the second is the serious one.

It is slow: Postgres produces and discards five hundred rows to return fifty, and the
cost grows with the page number. On a hypertable spanning months that is the difference
between a page and a timeout.

And it is **wrong while rows are arriving**. Detections and sightings are written
continuously, so a client reading page 1 and then page 2 counts its offset from a list
that has shifted underneath it. Two consequences, both invisible in testing because test
data does not move:

* It re-reads rows it already has -- a few new arrivals push the page-2 window back
  over the tail of page 1.
* On a busy table it may never reach older rows at all. If arrivals outpace paging,
  every `OFFSET n` lands inside the band of new rows, and the client walks *forwards*
  through fresh data while believing it is walking backwards through history.

What it does not do on an append-only table is silently skip rows -- prepending pushes
them later rather than past the window, and skipping needs deletions. That distinction
is worth keeping straight, because overstating the failure is how a reviewer stops
believing the rest of the argument.

A cursor is a position in a stable ordering, so a page boundary means the same thing
before and after new rows arrive.

The ordering must be total
---------------------------
`observed_at_utc` alone is not unique: two cameras produce detections in the same
millisecond regularly, and a cursor on a non-unique column either skips rows or repeats
them at every page boundary. Every cursor here is `(timestamp, id)`, which is total
because `id` is a UUID primary key.

Cursors are opaque, and that is a contract
-------------------------------------------
The encoded form is base64 of a JSON object, which anyone can decode in a browser
console -- it is not a secret and is not presented as one. It is *opaque* in the sense
that its structure is not part of the API contract: a client that parses it and
constructs its own will break when a sort key is added, and the documentation says so.
What it is not is guessable-into-another-tenant: the cursor carries only a position,
never a filter, so a forged cursor still reads through the caller's own RLS scope.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from services.api.errors import ApiError, ErrorCode

#: Page size when the client does not ask. Large enough that a console screen is one
#: request, small enough that a mistake costs little.
DEFAULT_PAGE_SIZE = 50

#: Ceiling. A client wanting everything should page; a single unbounded request against
#: a hypertable is how one caller spends the whole instance's memory.
MAX_PAGE_SIZE = 500


@dataclass(frozen=True)
class Cursor:
    """A position in a `(timestamp, id)` ordering."""

    observed_at: datetime
    row_id: uuid.UUID

    def encode(self) -> str:
        payload = json.dumps(
            {"t": self.observed_at.isoformat(), "i": str(self.row_id)}, separators=(",", ":")
        )
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, raw: str) -> "Cursor":
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            return cls(
                observed_at=datetime.fromisoformat(payload["t"]),
                row_id=uuid.UUID(payload["i"]),
            )
        except (
            binascii.Error,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            # A specific code, because a client that built its own cursor needs to know
            # that is why -- not to receive a generic validation failure it will blame
            # on its filters.
            raise ApiError(
                ErrorCode.CURSOR_INVALID,
                "cursor is not one this API issued; use the `next_cursor` from the "
                "previous page rather than constructing one",
            ) from exc


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page, and how to ask for the next."""

    items: list[T]
    next_cursor: str | None
    #: True when another page exists. Distinct from `next_cursor is not None` only in
    #: intent, but clients branch on a boolean more reliably than on a null check.
    has_more: bool
    page_size: int

    def as_dict(self, serialiser: Any = None) -> dict[str, Any]:
        return {
            "items": [serialiser(i) for i in self.items] if serialiser else self.items,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
            "page_size": self.page_size,
        }


def clamp_page_size(requested: int | None) -> int:
    if requested is None:
        return DEFAULT_PAGE_SIZE
    if requested < 1:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "page size must be at least 1")
    # Clamped rather than refused. A client asking for 10,000 wants everything and will
    # cope with 500 a page; refusing teaches them nothing and costs them a round trip.
    return min(requested, MAX_PAGE_SIZE)


def where_clause(*, timestamp_column: str, id_column: str, descending: bool = True) -> str:
    """The SQL fragment that resumes from a cursor.

    A row tuple comparison, not `timestamp < :t OR (timestamp = :t AND id < :i)`. The
    tuple form is both shorter and the one Postgres can satisfy from a composite index;
    the expanded form with an OR commonly degrades to a scan.
    """
    operator = "<" if descending else ">"
    return f"({timestamp_column}, {id_column}) {operator} (:cursor_t, :cursor_i)"


def build_page(
    rows: list[T],
    *,
    page_size: int,
    timestamp_of: Any,
    id_of: Any,
) -> Page[T]:
    """Turn `page_size + 1` fetched rows into a page.

    The caller fetches one more row than it needs. That extra row is how `has_more` is
    known without a second `COUNT(*)` -- which on a hypertable is the expensive query
    the pagination was supposed to avoid.
    """
    has_more = len(rows) > page_size
    items = rows[:page_size]
    next_cursor = (
        Cursor(observed_at=timestamp_of(items[-1]), row_id=id_of(items[-1])).encode()
        if has_more and items
        else None
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more, page_size=page_size)


def cursor_params(cursor: str | None) -> dict[str, Any]:
    """Bind parameters for `where_clause`, or an empty dict for the first page."""
    if not cursor:
        return {}
    decoded = Cursor.decode(cursor)
    return {"cursor_t": decoded.observed_at, "cursor_i": decoded.row_id}


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Cursor",
    "Page",
    "build_page",
    "clamp_page_size",
    "cursor_params",
    "where_clause",
]
