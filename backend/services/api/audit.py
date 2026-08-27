"""Tamper-evident audit ledger.

    entry_hash = SHA256(prev_hash || canonical_json(entry))

Each entry commits to the entire history before it, so altering any historical row
invalidates every hash from that point on. The chain is verifiable without trusting
the application that wrote it, which is the property that makes an evidence trail
worth anything in front of a forensic reviewer.

Canonicalisation matters as much as the hash. Two JSON encodings of the same object
must produce the same bytes, or verification fails on rows nobody touched: keys are
sorted, separators are fixed, non-ASCII is preserved rather than escaped, and
datetimes serialise to ISO-8601 in UTC.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from services.registry.models import AuditEntry

# The chain's anchor. 32 zero bytes -- the "previous hash" of the first entry ever
# written, so genesis is not a special case in the verification loop.
GENESIS_HASH = b"\x00" * 32


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        # Normalise to UTC before formatting: the same instant written from two
        # timezones must hash identically.
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"cannot canonicalise {type(obj).__name__} for the audit chain")


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes for a payload. Any change here breaks every stored hash."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    ).encode("utf-8")


def compute_hash(prev_hash: bytes, payload: dict[str, Any]) -> bytes:
    return hashlib.sha256(prev_hash + canonical_json(payload)).digest()


def _payload_of(entry: AuditEntry) -> dict[str, Any]:
    """The fields covered by the hash.

    `seq` is included so rows cannot be reordered, and `entry_hash` is excluded
    because it is the output. Everything an auditor would care about is inside.
    """
    return {
        "seq": entry.seq,
        "occurred_at": entry.occurred_at,
        "actor_id": entry.actor_id,
        "actor_role": entry.actor_role,
        "action": entry.action,
        "subject_type": entry.subject_type,
        "subject_id": entry.subject_id,
        "purpose": entry.purpose,
        "detail": entry.detail,
    }


def append(
    session: Session,
    *,
    action: str,
    subject_type: str,
    subject_id: str,
    actor_id: str | None = None,
    actor_role: str | None = None,
    purpose: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditEntry:
    """Append one entry, linked to the current tail.

    Flushes to obtain `seq` before hashing, because `seq` is part of the payload.
    The caller's transaction still governs commit, so an audit entry and the change
    it records either both land or neither does -- an audit trail that can disagree
    with the data it describes is worse than none.
    """
    tail = session.execute(
        select(AuditEntry).order_by(AuditEntry.seq.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = tail.entry_hash if tail is not None else GENESIS_HASH

    # Reserve the sequence value *before* inserting, so the row can be written once
    # with its final hash. The earlier implementation inserted a placeholder hash and
    # updated it after the flush assigned `seq`, which meant the application needed
    # UPDATE on the ledger -- and a tamper-evident log that its own writer can update
    # is defensible only by convention. Taking the number up front makes the table
    # genuinely append-only, so the privilege can be withheld at the database level.
    seq = session.execute(
        text("SELECT nextval(pg_get_serial_sequence('audit_entry', 'seq'))")
    ).scalar_one()

    entry = AuditEntry(
        seq=int(seq),
        occurred_at=datetime.now(timezone.utc),
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        subject_type=subject_type,
        subject_id=str(subject_id),
        purpose=purpose,
        detail=detail or {},
        prev_hash=prev_hash,
        entry_hash=GENESIS_HASH,  # replaced below, before the row is ever written
    )
    entry.entry_hash = compute_hash(prev_hash, _payload_of(entry))

    session.add(entry)
    session.flush()
    return entry


class ChainBreak(dict[str, object]):
    """One detected inconsistency. A dict so it serialises straight to JSON."""


def verify_chain(session: Session, limit: int | None = None) -> dict[str, Any]:
    """Recompute the whole chain and report the first break, if any."""
    stmt = select(AuditEntry).order_by(AuditEntry.seq.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    entries = list(session.execute(stmt).scalars())

    breaks: list[ChainBreak] = []
    expected_prev = GENESIS_HASH

    for entry in entries:
        if entry.prev_hash != expected_prev:
            breaks.append(
                ChainBreak(
                    seq=entry.seq,
                    kind="broken_link",
                    detail=(
                        "prev_hash does not match the preceding entry's hash; "
                        "an entry was inserted, removed or reordered"
                    ),
                )
            )
        recomputed = compute_hash(entry.prev_hash, _payload_of(entry))
        if recomputed != entry.entry_hash:
            breaks.append(
                ChainBreak(
                    seq=entry.seq,
                    kind="content_modified",
                    detail="entry_hash does not match the entry's contents",
                )
            )
        expected_prev = entry.entry_hash

    return {
        "valid": not breaks,
        "entries_checked": len(entries),
        "breaks": breaks,
        "head_hash": entries[-1].entry_hash.hex() if entries else None,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
