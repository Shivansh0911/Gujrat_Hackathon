#!/usr/bin/env python
"""Resolve floating container tags in the compose files to immutable digests.

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

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from services.common.paths import PROJECT_ROOT  # noqa: E402

# Both stacks, not just the development one. The production compose file is the one a
# deployment actually runs, so a floating tag there is the more consequential of the
# two. An earlier version of this script resolved its root to `backend/` after the
# backend/frontend split, so the CI job failed on a missing file rather than on an
# unpinned image -- and a check that cannot find what it checks is not a check.
COMPOSE_FILES = [
    PROJECT_ROOT / "docker-compose.yml",
    PROJECT_ROOT / "docker-compose.prod.yml",
]

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


def _via_buildx(ref: str) -> str | None:
    """Resolve through `buildx imagetools`, which needs no experimental flag."""
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref,
         "--format", "{{.Manifest.Digest}}"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if proc.returncode != 0:
        return None
    digest = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else ""
    return digest if digest.startswith("sha256:") else None


def _via_manifest(ref: str) -> str | None:
    """Resolve through `docker manifest inspect`, which may need experimental mode."""
    proc = subprocess.run(
        ["docker", "manifest", "inspect", "--verbose", ref],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    entries = data if isinstance(data, list) else [data]
    digest = entries[0].get("Descriptor", {}).get("digest")
    return str(digest) if digest else None


def resolve_digest(ref: str, attempts: int = 3) -> str | None:
    """Ask the registry for the manifest digest of `ref`.

    `buildx imagetools` is tried first: it is the supported path on current Docker,
    it needs no experimental flag, and it returns the digest of the OCI image index
    the tag actually resolves to. `docker manifest inspect` is kept as a fallback for
    older CLIs, but note that on a registry serving both an OCI index and a legacy
    Docker manifest list for one tag the two commands return *different* digests.
    Both are valid pins; they are not interchangeable, so we prefer one consistently
    rather than taking whichever answered first.
    """
    for _ in range(attempts):
        for resolver in (_via_buildx, _via_manifest):
            try:
                digest = resolver(ref)
            except subprocess.TimeoutExpired:
                continue
            if digest:
                return digest
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if any image is unpinned")
    mode.add_argument("--write", action="store_true", help="resolve and rewrite in place")
    args = ap.parse_args()

    present = [c for c in COMPOSE_FILES if c.exists()]
    if not present:
        print("FAIL: no compose file found - has the layout changed?")
        return 1

    total = 0
    total_unpinned = 0
    failed: list[str] = []
    rewrote: list[str] = []

    for compose in present:
        text = compose.read_text(encoding="utf-8")
        images = parse_images(text)
        if not images:
            print(f"FAIL: no image references in {compose.name}")
            return 1

        print("")
        print(compose.name)
        total += len(images)

        if args.check:
            for ref, digest in images:
                label = "PINNED  " if digest else "UNPINNED"
                print(f"  {label} {ref}")
            total_unpinned += sum(1 for _, d in images if d is None)
            continue

        updated = text
        for ref, digest in images:
            if digest is not None:
                continue
            print(f"  resolving {ref} ...", flush=True)
            new_digest = resolve_digest(ref)
            if new_digest is None:
                failed.append(ref)
                print(f"    could not resolve {ref}")
                continue
            # Replace the bare reference, leaving any trailing comment in place.
            updated = re.sub(
                r"(image:\s*)" + re.escape(ref) + r"(?!@)",
                lambda m, r=ref, d=new_digest: m.group(1) + r + "@" + d,
                updated,
            )
            print(f"    {ref} -> {new_digest}")
        if updated != text:
            compose.write_text(updated, encoding="utf-8")
            rewrote.append(compose.name)

    if args.check:
        if total_unpinned:
            print("")
            print(
                f"FAIL: {total_unpinned} of {total} image(s) across "
                f"{len(present)} compose file(s) are not digest-pinned."
            )
            print("Run `make pin-digests` on a connection with registry access.")
            return 1
        print("")
        print(
            f"PASS: all {total} images across {len(present)} "
            "compose file(s) are digest-pinned."
        )
        return 0

    print("")
    if rewrote:
        print(f"rewrote: {', '.join(rewrote)}")
    elif not failed:
        print("nothing to do: every image already carries a digest")
    if failed:
        print(f"{len(failed)} image(s) unresolved: {', '.join(failed)}")
        print("Left unpinned deliberately - a fabricated digest is worse than a tag.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
