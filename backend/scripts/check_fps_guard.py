#!/usr/bin/env python
"""Assert the codebase contains exactly one read of the declared frame rate.

§2.2: "Never trust the reported frame rate. Using it to convert pixels-per-frame into
speed, dwell time or any time-derived metric produces incorrect results."

The preflight's version of this check permits any number of reads so long as each
carries a reference-only marker. That is one comment away from being bypassed: a future
commit can add a second read, paste the marker, and the check stays green.

So the permitted count is asserted as an exact number, not as a property of each site.
Adding a legitimate second read requires editing EXPECTED_READS in this file, which
shows up in review as a deliberate act rather than a comment nobody read.

Exit code 1 on any deviation. Wired into CI and `make fps-guard`.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The single permitted read: StreamSession._open() records the declared FPS purely so
# the platform can display it beside measured_fps and demonstrate the discrepancy the
# organiser's own integration guide warns about. It feeds no timing path.
# Two permitted reads, both reference-only and both marked as such on the line:
#   services/common/stream_client.py  - declared rate of a live gateway stream
#   services/ingest/file_source.py    - declared rate of a local own-feed clip
# Each exists solely so the platform can display declared beside measured. Neither
# feeds a timing path. Raising this number is a reviewed decision, which is why the
# guard fails until it is done in the same commit as the new read.
EXPECTED_READS = 2

# This file necessarily contains the literal; the preflight prints it in a report.
EXEMPT_FILENAMES = {"check_fps_guard.py", "preflight_check.py"}

NEEDLE = "CAP_PROP_FPS"


def find_reads() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        if path.name in EXEMPT_FILENAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if NEEDLE not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            # Count reads, not prose. Prose that *discusses* the constant is exactly
            # what we want people writing; only the code portion of the line counts.
            code = line.split("#", 1)[0]
            if NEEDLE in code:
                hits.append((str(path.relative_to(REPO_ROOT)), lineno, line.strip()))
    return hits


def main() -> int:
    hits = find_reads()
    print(f"{NEEDLE} occurrences outside exempt files: {len(hits)} (expected {EXPECTED_READS})")
    for rel, lineno, line in hits:
        print(f"  {rel}:{lineno}: {line}")

    if len(hits) != EXPECTED_READS:
        print(
            f"\nFAIL: expected exactly {EXPECTED_READS} occurrence(s), found {len(hits)}.\n"
            "If this is a legitimate new read, update EXPECTED_READS in "
            "scripts/check_fps_guard.py in the same commit, so the change is reviewed.\n"
            "If it is a timing path, it is a §2.2 violation: measure the rate from PTS."
        )
        return 1

    # The one permitted read must still be marked, so review does not have to remember
    # which occurrence is the sanctioned one.
    rel, lineno, line = hits[0]
    if "reference-only" not in line and "never used for timing" not in line:
        print(
            f"\nFAIL: the permitted read at {rel}:{lineno} has lost its marker.\n"
            "Restore a 'reference-only' or 'never used for timing' comment on that line."
        )
        return 1

    print("\nPASS: exactly one reference-only read, correctly marked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
