#!/usr/bin/env python
"""Resolve floating container tags in docker-compose.yml to immutable digests.

A tag is mutable. `redis:7.4.1-alpine` today and the same tag in three months can be
different bytes, so a jury re-running our stack after the submission window would not
be running what we tested. Digest pinning makes the deployment reproducible.

Two modes:
  --check   exit non-zero if any image lacks a digest. Used by CI.
  --write   resolve each tag against its registry and rewrite the file in place.

`--write` needs registry access and a working `docker` CLI. When registries are
unreachable it reports which images it could not resolve and leaves the file untouched
rather than guessing -- a fabricated digest is worse than an honest tag.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"

# Matches `image: <ref>` capturing the reference, with or without a digest.
IMAGE_RE = re.compile(r"^(?P<indent>\s*)image:\s*(?P<ref>\S+)(?P<rest>.*)$", re.MULTILINE)


def parse_images(text: str) -> list[tuple[str, str | None]]:
    """Return (reference_without_digest, digest_or_None) for each image line."""
    out: list[tuple[str, str | None]] = []
    for m in IMAGE_RE.finditer(text):
        ref = m.group("ref")
        if "@sha256:" in ref:
            base, digest = ref.split("@", 1)
            out.append((base, digest))
        else:
            out.append((ref, None))
    return out


def resolve_digest(ref: str, attempts: int = 3) -> str | None:
    """Ask the registry for the manifest digest of `ref`."""
    for _ in range(attempts):
        proc = subprocess.run(
            ["docker", "manifest", "inspect", "--verbose", ref],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if proc.returncode != 0:
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else [data]
        digest = entries[0].get("Descriptor", {}).get("digest")
        if digest:
            return str(digest)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if any image is unpinned")
    mode.add_argument("--write", action="store_true", help="resolve and rewrite in place")
    args = ap.parse_args()

    if not COMPOSE.exists():
        print(f"FAIL: {COMPOSE} not found")
        return 1

    text = COMPOSE.read_text(encoding="utf-8")
    images = parse_images(text)
    if not images:
        print("FAIL: no image references found — has the compose file moved?")
        return 1

    unpinned = [ref for ref, digest in images if digest is None]

    if args.check:
        for ref, digest in images:
            print(f"  {'PINNED  ' if digest else 'UNPINNED'} {ref}")
        if unpinned:
            print(
                f"\nFAIL: {len(unpinned)} image(s) are not digest-pinned: "
                f"{', '.join(unpinned)}\n"
                "Run `make pin-digests` on a connection with registry access."
            )
            return 1
        print(f"\nPASS: all {len(images)} images are digest-pinned.")
        return 0

    # --write
    updated = text
    resolved, failed = 0, []
    for ref, digest in images:
        if digest is not None:
            continue
        print(f"resolving {ref} ...", flush=True)
        new_digest = resolve_digest(ref)
        if new_digest is None:
            failed.append(ref)
            print(f"  could not resolve {ref}")
            continue
        # Replace the bare reference, leaving any trailing comment in place.
        updated = re.sub(
            rf"(image:\s*){re.escape(ref)}(?!@)",
            rf"\g<1>{ref}@{new_digest}",
            updated,
        )
        resolved += 1
        print(f"  {ref} -> {new_digest}")

    if resolved:
        COMPOSE.write_text(updated, encoding="utf-8")
        print(f"\nrewrote {COMPOSE.name}: {resolved} image(s) pinned")
    if failed:
        print(
            f"\n{len(failed)} image(s) unresolved: {', '.join(failed)}\n"
            "Left unpinned deliberately — a fabricated digest is worse than a tag."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
