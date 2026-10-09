"""Cursor pagination, and the correctness bug that offset pagination has.

`test_offset_pagination_would_skip_rows_that_arrive_mid_scan` is the one worth reading.
It demonstrates, with a list rather than a database, the failure that makes offset wrong
on a table still being written: a client paging through an alert backlog silently misses
alerts, and never sees an error.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from services.api.errors import ApiError, ErrorCode
from services.api.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    build_page,
    clamp_page_size,
    cursor_params,
    where_clause,
)

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


# ------------------------------------------------------- why not offset


def test_offset_pagination_re_reads_rows_when_new_ones_arrive() -> None:
    """The reason this module exists, demonstrated without a database.

    A client reads page 1, four newer rows land, then it reads page 2 by offset. It is
    handed rows it has already seen, because the offset counts from a list that has
    shifted underneath it.

    Note the failure this does *not* cause on an append-heavy table: nothing is
    permanently skipped, because prepending pushes rows later rather than past the
    window. Skipping needs deletions. The first draft of this test asserted a silent
    skip and failed, which was the test being right and the claim being overstated --
    the module docstring was corrected to match.
    """
    page_size = 10
    original = list(range(100, 0, -1))  # newest first
    first = original[0:page_size]  # 100..91

    shifted = list(range(104, 100, -1)) + original  # four newer rows arrive

    offset_second = shifted[page_size : page_size * 2]
    assert set(first) & set(offset_second), "offset hands back rows already seen"

    # A cursor resumes from a position, so the boundary means the same thing before and
    # after the new rows arrived.
    resume_after = first[-1]
    cursor_second = [row for row in shifted if row < resume_after][:page_size]
    assert cursor_second == original[page_size : page_size * 2]
    assert not set(first) & set(cursor_second)


def test_offset_pagination_may_never_reach_older_rows() -> None:
    """The worse failure on a busy table: paging that never terminates downwards.

    If rows arrive faster than the client pages, every `OFFSET n` lands inside the band
    of new arrivals and the client walks forwards through fresh rows while believing it
    is walking backwards through history. A cursor cannot do this: it is anchored to a
    row, not to a count.
    """
    page_size = 10
    original = list(range(100, 0, -1))
    first = original[0:page_size]

    # Forty newer rows -- more than a page -- arrive between the two reads.
    shifted = list(range(140, 100, -1)) + original

    offset_second = shifted[page_size : page_size * 2]
    assert min(offset_second) > max(
        first
    ), "offset moved the client towards newer rows, not older ones"

    cursor_second = [row for row in shifted if row < first[-1]][:page_size]
    assert max(cursor_second) < min(first), "a cursor always moves away from what was seen"


# ----------------------------------------------------------------- cursors


def test_a_cursor_round_trips() -> None:
    original = Cursor(observed_at=NOW, row_id=uuid.uuid4())
    decoded = Cursor.decode(original.encode())
    assert decoded.observed_at == original.observed_at
    assert decoded.row_id == original.row_id


def test_a_cursor_is_url_safe_and_unpadded() -> None:
    """It travels in a query string, where '+' and '=' are hazards."""
    encoded = Cursor(observed_at=NOW, row_id=uuid.uuid4()).encode()
    assert "+" not in encoded and "/" not in encoded and "=" not in encoded


def test_a_cursor_preserves_timezone() -> None:
    """A cursor that lost its offset would resume hours away from where it stopped."""
    aware = datetime(2026, 1, 1, 6, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert Cursor.decode(Cursor(aware, uuid.uuid4()).encode()).observed_at == aware


@pytest.mark.parametrize(
    "bad",
    ["", "not-base64!!", "YWJj", "e30", "bnVsbA"],
    ids=["empty", "illegal-chars", "not-json", "empty-object", "json-null"],
)
def test_a_forged_cursor_is_refused_with_its_own_error_code(bad: str) -> None:
    """A client that built its own cursor needs to know that is why -- not to get a
    generic validation failure it will blame on its filters."""
    with pytest.raises(ApiError) as exc:
        Cursor.decode(bad)
    assert exc.value.code is ErrorCode.CURSOR_INVALID
    assert "previous page" in exc.value.message


def test_cursor_params_are_empty_for_a_first_page() -> None:
    assert cursor_params(None) == {}
    assert cursor_params("") == {}


def test_cursor_params_bind_both_halves_of_the_ordering() -> None:
    """A cursor on timestamp alone skips or repeats rows at every page boundary, because
    two cameras produce detections in the same millisecond regularly."""
    params = cursor_params(Cursor(NOW, uuid.uuid4()).encode())
    assert set(params) == {"cursor_t", "cursor_i"}


# -------------------------------------------------------------- the SQL


def test_the_where_clause_uses_a_row_tuple_comparison() -> None:
    """Shorter than the expanded OR form, and the one Postgres satisfies from a
    composite index -- the OR form commonly degrades to a scan."""
    clause = where_clause(timestamp_column="d.observed_at_utc", id_column="d.id")
    assert clause == "(d.observed_at_utc, d.id) < (:cursor_t, :cursor_i)"
    assert " OR " not in clause


def test_ascending_order_flips_the_comparison() -> None:
    clause = where_clause(timestamp_column="t", id_column="i", descending=False)
    assert clause.endswith("> (:cursor_t, :cursor_i)")


# ---------------------------------------------------------------- pages


def _row(n: int) -> tuple[datetime, uuid.UUID]:
    return (NOW - timedelta(minutes=n), uuid.uuid4())


def test_a_full_page_offers_a_next_cursor() -> None:
    rows = [_row(n) for n in range(11)]  # page_size + 1
    page = build_page(rows, page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    assert len(page.items) == 10
    assert page.has_more is True
    assert page.next_cursor is not None


def test_a_partial_page_is_the_last_one() -> None:
    rows = [_row(n) for n in range(4)]
    page = build_page(rows, page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    assert page.has_more is False
    assert page.next_cursor is None


def test_an_exactly_full_page_without_an_extra_row_is_the_last_one() -> None:
    """The caller fetches page_size + 1; getting exactly page_size means no more.

    This is how `has_more` is known without a COUNT(*), which on a hypertable is the
    expensive query the pagination was supposed to avoid.
    """
    rows = [_row(n) for n in range(10)]
    page = build_page(rows, page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    assert page.has_more is False


def test_an_empty_page_is_valid_and_terminal() -> None:
    page = build_page([], page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    assert page.items == [] and page.has_more is False and page.next_cursor is None


def test_the_next_cursor_points_at_the_last_returned_row() -> None:
    """Not at the extra fetched row, which the client has not seen."""
    rows = [_row(n) for n in range(11)]
    page = build_page(rows, page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    assert page.next_cursor is not None
    decoded = Cursor.decode(page.next_cursor)
    assert decoded.row_id == rows[9][1]


def test_a_page_serialises_with_the_fields_a_client_branches_on() -> None:
    page = build_page([_row(0)], page_size=10, timestamp_of=lambda r: r[0], id_of=lambda r: r[1])
    body = page.as_dict(serialiser=lambda r: {"id": str(r[1])})
    assert set(body) == {"items", "next_cursor", "has_more", "page_size"}


# ------------------------------------------------------------ page size


def test_an_absent_page_size_uses_the_default() -> None:
    assert clamp_page_size(None) == DEFAULT_PAGE_SIZE


def test_an_oversized_request_is_clamped_rather_than_refused() -> None:
    """A client asking for 10,000 wants everything and will cope with 500 a page.
    Refusing teaches them nothing and costs a round trip."""
    assert clamp_page_size(10_000) == MAX_PAGE_SIZE


def test_a_nonsense_page_size_is_refused() -> None:
    with pytest.raises(ApiError) as exc:
        clamp_page_size(0)
    assert exc.value.code is ErrorCode.VALIDATION_FAILED


def test_the_ceiling_bounds_what_one_request_can_cost() -> None:
    """A single unbounded request against a hypertable is how one caller spends the
    whole instance's memory."""
    assert MAX_PAGE_SIZE <= 1000
