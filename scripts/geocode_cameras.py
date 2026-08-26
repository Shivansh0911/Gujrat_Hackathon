#!/usr/bin/env python
"""Populate data/seed/camera_geo.csv with coordinates that trace to a real source.

The gateway catalogue publishes no coordinates (re-checked on every run by
`_coords_from_catalogue`), so positions are resolved by geocoding the free-text
`location` string against Nominatim (OpenStreetMap).

**No coordinate in the output is ever invented.** Every row traces to either a
geocoder response stored in data/seed/geocode_cache.json, or to a named district
centroid that is itself a cached geocoder response. A camera we cannot resolve stays
`unset` with null geometry -- an honest absence, because a camera with a fabricated
position produces an authoritative-looking route that is wrong, which is precisely
what a forensic reviewer is looking for.

Provenance tiers, which route reconstruction consumes as a tolerance radius:

  published     50 m   coordinate supplied by the organiser
  geocoded     300 m   a specific, confident Nominatim hit
  approximate 5000 m   district centroid; the district is known, the spot is not
  unset           --   nothing resolvable; excluded from spatial queries

The cache is committed so this runs once and there is no external dependency at
demo time.

Usage:
    python scripts/geocode_cameras.py [--refresh] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from services.common import redact  # noqa: E402
from services.common.catalogue import fetch_catalogue  # noqa: E402
from services.common.config import get_settings  # noqa: E402

log = logging.getLogger("geocode")

SEED_CSV = REPO_ROOT / "data" / "seed" / "camera_geo.csv"
CACHE_PATH = REPO_ROOT / "data" / "seed" / "geocode_cache.json"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent and at most 1 req/s.
# Violating either gets the project blocked, which at demo time would be terminal.
USER_AGENT = (
    "SETU-GujaratPoliceHackathon/1.0 "
    "(camera registry geocoding; contact via competition submission record)"
)
RATE_LIMIT_S = 1.1

RADIUS_PUBLISHED = 50.0
RADIUS_GEOCODED = 300.0
RADIUS_APPROXIMATE = 5000.0

# Gujarat district NAMES, used for parsing location strings. Their coordinates are
# still fetched from the geocoder and cached -- never written from memory.
GUJARAT_DISTRICTS = [
    "Ahmedabad", "Amreli", "Anand", "Aravalli", "Banaskantha", "Bharuch", "Bhavnagar",
    "Botad", "Chhota Udaipur", "Dahod", "Dang", "Devbhoomi Dwarka", "Gandhinagar",
    "Gir Somnath", "Jamnagar", "Junagadh", "Kutch", "Kheda", "Mahisagar", "Mehsana",
    "Morbi", "Narmada", "Navsari", "Panchmahal", "Patan", "Porbandar", "Rajkot",
    "Sabarkantha", "Surat", "Surendranagar", "Tapi", "Vadodara", "Valsad",
]

# Tokens appearing in catalogue location strings that imply a district. Parsing hints
# only -- each still resolves through the geocoder.
TOKEN_HINTS: dict[str, str] = {
    "junagadh": "Junagadh",
    "somnath": "Gir Somnath",
    "rajkot": "Rajkot",
    "navsari": "Navsari",
    "gandevi": "Navsari",
    "khaparia": "Navsari",
    "bilimora": "Navsari",
    "patan": "Patan",
    "dehgam": "Gandhinagar",
    "adalaj": "Gandhinagar",
    "gandhidham": "Kutch",
    "mervada": "Banaskantha",
}

# The catalogue's first block is unlabelled, but these are Ahmedabad street names.
# Used only to choose which CITY to geocode within; the coordinate still comes from
# Nominatim.
AHMEDABAD_HINTS = {
    "chiman bhai", "janpath", "o.n.g.c", "ongc", "paldi", "visat", "cn vidhyalaya",
    "delight", "suvidha park", "mohanpura",
}


@dataclass
class Resolution:
    lat: float | None
    lon: float | None
    source: str
    radius_m: float | None
    resolved_by: str
    note: str


class Cache:
    """Every geocoder response, keyed by the exact query string."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.data: dict[str, object] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def __contains__(self, key: str) -> bool:
        return key in self.data


_last_request = 0.0


def nominatim_search(query: str, cache: Cache, refresh: bool = False):
    """Query Nominatim once per unique string, honouring the rate limit."""
    global _last_request
    if not refresh and query in cache:
        return cache.get(query)

    elapsed = time.monotonic() - _last_request
    if elapsed < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - elapsed)

    log.info("geocoding: %s", query)
    try:
        resp = requests.get(
            NOMINATIM,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 5,
                "countrycodes": "in",
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=30,
        )
        _last_request = time.monotonic()
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # A geocoder failure is recorded as a failure, never converted into a guess.
        log.warning("geocode failed for %r: %s", query, exc)
        results = {"__error__": f"{type(exc).__name__}: {exc}"}

    cache.put(query, results)
    cache.save()
    return results


def _clean_location(text: str) -> str:
    """Strip the catalogue's leading index number and separators."""
    t = re.sub(r"^\s*\d+\s+", "", text.strip())
    t = t.replace("-", " ").replace("_", " ")
    t = re.sub(r"\s+", " ", t)
    return t.strip(" ,")


def infer_district(location_text: str) -> str | None:
    low = location_text.lower()
    for token, district in TOKEN_HINTS.items():
        if token in low:
            return district
    for district in GUJARAT_DISTRICTS:
        if district.lower() in low:
            return district
    if any(h in low for h in AHMEDABAD_HINTS):
        return "Ahmedabad"
    return None


# Nominatim's place_rank encodes how specific a result is: ~4-12 administrative
# areas, 16 town/city, 19-20 village/suburb/neighbourhood, 26 road, 30 building.
# The radius we record scales with it, so route plausibility gating knows how much
# slack a coordinate deserves instead of treating every hit as equally precise.
RANK_ROAD_OR_FINER = 26
RANK_SUBURB_OR_FINER = 19
RANK_TOWN_OR_FINER = 16

RADIUS_BY_RANK = [
    (RANK_ROAD_OR_FINER, 300.0),      # a specific road or building
    (RANK_SUBURB_OR_FINER, 1500.0),   # a named suburb or village
    (RANK_TOWN_OR_FINER, 4000.0),     # a town centre
]

# Words that describe the KIND of place rather than naming it. Indian junction and
# facility terms defeat Nominatim when left in the query: "Visat teen Rasta" finds
# nothing, "Visat" resolves. ("teen/char/tran rasta" are three/four/three-way
# junctions; "tollnaka" a toll plaza; "char chowk" a crossroads.)
NOISE_TOKENS = [
    "teen rasta", "char rasta", "tran rasta", "char chowk", "chowk",
    "tollnaka", "toll naka", "gram panchayat", "taluka", "district",
    "bus port", "showroom", "circle", "bypass", "near by", "gate",
    "cctv", "office", "road", "bridge", "new", "p2", "p1",
]


def _strip_noise(text: str) -> str:
    """Remove place-kind words, keeping the part that actually names somewhere."""
    out = text.lower()
    for token in NOISE_TOKENS:
        out = re.sub(rf"\b{re.escape(token)}\b", " ", out)
    out = re.sub(r"\b\d+\b", " ", out)          # stray index numbers
    out = re.sub(r"\s+", " ", out).strip(" ,-")
    return out


def _candidate_queries(location_text: str, district: str | None) -> list[str]:
    """Progressively simpler queries, most specific first.

    Nominatim does not do fuzzy matching on compound strings, so a single attempt at
    the full label mostly returns nothing. Trying the stripped name next is what
    turns a district centroid into a real position.
    """
    cleaned = _clean_location(location_text)
    stripped = _strip_noise(cleaned)
    suffix = f", {district}, Gujarat, India" if district else ", Gujarat, India"

    candidates: list[str] = []
    if cleaned:
        candidates.append(f"{cleaned}{suffix}")
    if stripped and stripped != cleaned.lower():
        candidates.append(f"{stripped}{suffix}")
        # The leading word alone is often the settlement name ("Timbavadi", "Mervada").
        head = stripped.split()[0] if stripped.split() else ""
        if head and head != stripped:
            candidates.append(f"{head}{suffix}")
    # De-duplicate, preserving order.
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def _rank_of(hit: dict) -> int:
    try:
        return int(hit.get("place_rank", 0))
    except (TypeError, ValueError):
        return 0


def _radius_for(rank: int) -> float | None:
    for threshold, radius in RADIUS_BY_RANK:
        if rank >= threshold:
            return radius
    return None


# Result types that genuinely locate a road position, in preference order. Picking the
# finest place_rank alone is wrong: "Janpath, Ahmedabad" returned a guest house at
# rank 30, which is precise and irrelevant. A camera watches a road or a locality, so
# those types are preferred and arbitrary POIs are only a last resort.
PREFERRED_TYPES = (
    ("place", {"suburb", "neighbourhood", "quarter", "city_block"}),
    ("highway", None),          # a named road or junction
    ("junction", None),
    ("place", {"town", "village", "hamlet"}),
)


def _type_priority(hit: dict) -> int:
    """Lower is better. Anything unlisted sorts last."""
    cls, typ = hit.get("class"), hit.get("type")
    for i, (want_cls, want_types) in enumerate(PREFERRED_TYPES):
        if cls == want_cls and (want_types is None or typ in want_types):
            return i
    return len(PREFERRED_TYPES)


def _name_matches(hit: dict, needle: str) -> bool:
    """Guard against a hit that is precise but about something else entirely."""
    if not needle:
        return True
    head = needle.split()[0]
    return head.lower() in str(hit.get("display_name", "")).lower()


def _choose_hit(hits: list[dict], needle: str) -> dict | None:
    """Best hit: right kind of place, name actually matching, then finest rank."""
    usable = [h for h in hits if _radius_for(_rank_of(h)) is not None]
    if not usable:
        return None
    named = [h for h in usable if _name_matches(h, needle)] or usable
    return min(named, key=lambda h: (_type_priority(h), -_rank_of(h)))


def resolve_camera(location_text: str, cache: Cache, refresh: bool) -> Resolution:
    district = infer_district(location_text)
    needle = _strip_noise(_clean_location(location_text))

    # Tier 2: try each candidate query, most specific phrasing first.
    for query in _candidate_queries(location_text, district):
        hits = nominatim_search(query, cache, refresh)
        if not isinstance(hits, list) or not hits:
            continue
        best = _choose_hit(hits, needle)
        if best is None:
            # Nominatim answered, but only with administrative areas. That is the
            # district fallback by another name, so let tier 3 own it explicitly.
            continue
        rank = _rank_of(best)
        display = str(best.get("display_name", ""))[:90]
        return Resolution(
            lat=float(best["lat"]),
            lon=float(best["lon"]),
            source="geocoded",
            radius_m=_radius_for(rank),
            resolved_by=f"nominatim:{query}",
            note=f"rank {rank} {best.get('class')}/{best.get('type')}: {display}",
        )

    # Tier 3: district centroid. Coarse but honest, and the radius says so.
    if district:
        dquery = f"{district} district, Gujarat, India"
        dhits = nominatim_search(dquery, cache, refresh)
        if isinstance(dhits, list) and dhits:
            hit = dhits[0]
            return Resolution(
                lat=float(hit["lat"]),
                lon=float(hit["lon"]),
                source="approximate",
                radius_m=RADIUS_APPROXIMATE,
                resolved_by=f"nominatim:{dquery}",
                note=f"district centroid ({district}); exact site unknown",
            )

    # Tier 4: nothing resolvable. Recorded as unknown, not guessed.
    return Resolution(None, None, "unset", None, "", "no confident geocoder result")


def _coords_from_catalogue() -> dict[str, tuple[float, float]]:
    """Tier 1: use organiser-published coordinates if the catalogue carries them.

    Re-checked on every run rather than assumed absent, so if the organisers add
    lat/lon fields we pick them up without a code change.
    """
    out: dict[str, tuple[float, float]] = {}
    try:
        cameras = fetch_catalogue(get_settings())
    except Exception as exc:  # noqa: BLE001 - third-party infrastructure
        log.warning("catalogue unavailable; cannot check for published coordinates: %s", exc)
        return out
    for cam in cameras:
        for lat_key, lon_key in (("lat", "lon"), ("latitude", "longitude")):
            lat = getattr(cam, lat_key, None)
            lon = getattr(cam, lon_key, None)
            if lat is not None and lon is not None:
                out[cam.external_id] = (float(lat), float(lon))
    log.info("catalogue published coordinates for %d camera(s)", len(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-query")
    ap.add_argument("--dry-run", action="store_true", help="do not write the CSV")
    args = ap.parse_args()

    redact.install(level=logging.INFO)
    cache = Cache(CACHE_PATH)
    published = _coords_from_catalogue()

    rows = list(csv.DictReader(SEED_CSV.open(encoding="utf-8")))
    log.info("resolving %d cameras", len(rows))

    now = datetime.now(timezone.utc).isoformat()
    counts: dict[str, int] = {}

    for row in rows:
        ref = row["camera_ref"]
        loc = row["location_text"]

        # Never overwrite a human's manual survey with a geocoder result.
        if row.get("geom_source") == "manual_survey" and row.get("lat"):
            counts["manual_survey"] = counts.get("manual_survey", 0) + 1
            continue

        if ref in published:
            lat, lon = published[ref]
            res = Resolution(lat, lon, "published", RADIUS_PUBLISHED,
                             "organiser catalogue", "published by organiser")
        else:
            res = resolve_camera(loc, cache, args.refresh)

        row["lat"] = f"{res.lat:.6f}" if res.lat is not None else ""
        row["lon"] = f"{res.lon:.6f}" if res.lon is not None else ""
        row["geom_source"] = res.source
        row["confidence_radius_m"] = f"{res.radius_m:.0f}" if res.radius_m else ""
        row["resolved_by"] = res.resolved_by
        row["resolved_at"] = now if res.source != "unset" else ""
        counts[res.source] = counts.get(res.source, 0) + 1
        log.info("  %-3s %-45s -> %-11s %s", ref, loc[:45], res.source, res.note[:70])

    print("\nResolution summary")
    print("-" * 60)
    for source in ("published", "geocoded", "approximate", "manual_survey", "unset"):
        if counts.get(source):
            print(f"  {source:<14} {counts[source]:>3}")
    print("-" * 60)
    print(f"  {'total':<14} {len(rows):>3}\n")

    if args.dry_run:
        print("dry run: CSV not written")
        return 0

    fields = ["camera_ref", "location_text", "lat", "lon", "geom_source",
              "confidence_radius_m", "resolved_by", "resolved_at"]
    with SEED_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {SEED_CSV}")
    print(f"cache: {CACHE_PATH} ({len(cache.data)} queries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
