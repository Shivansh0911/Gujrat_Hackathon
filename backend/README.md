# SETU backend

Python services behind the console: camera registry, feed ingest, ANPR, watchlist
matching and route reconstruction.

## Running

From the **project root** (the virtualenv and `.env` live there):

```bash
make up          # Postgres 16 + PostGIS + TimescaleDB + pgvector
make migrate     # Alembic to head
make seed        # departments + camera coordinates
make api         # http://127.0.0.1:8090/docs
make test
```

Run scripts from inside `backend/` so `services.*` resolves:

```bash
cd backend
../.venv/Scripts/python.exe scripts/preflight_check.py      # feed-contract checks
../.venv/Scripts/python.exe scripts/probe_catalogue.py      # measure real stream properties
../.venv/Scripts/python.exe scripts/run_anpr.py <video> --persist
```

## Layout

| Package | Responsibility |
|---|---|
| `services/api/` | FastAPI app, routers, JWT auth, scoped accessors, hash-chained audit |
| `services/analytics/` | ANPR pipeline, Indian plate grammar, watchlist matcher, detection persistence |
| `services/ingest/` | `CameraSource` protocol with `FileSource` and `GatewaySource` |
| `services/registry/` | SQLAlchemy models, camera lifecycle state machine, seed loader |
| `services/common/` | Transport selection, stream client, SSRF guard, log redaction, paths |
| `migrations/` | Alembic. Every migration reversible; the round trip is tested, not assumed |
| `scripts/` | Preflight, catalogue probe, geocoding, ANPR runner, demo seeding |
| `tests/` | 126 tests. Database tests skip cleanly when Postgres is unreachable |

## Invariants that must not be broken

**Timing comes from stream PTS only** — never a declared frame rate, never frame
arrival time. The gateway replays a buffered GOP on connect, so arrival-time
reasoning produces impossible velocities in the first seconds of every session.
`scripts/check_fps_guard.py` enforces this in CI: exactly two `CAP_PROP_FPS` reads
are permitted, both marked reference-only, and adding a third fails the build until
the count is raised deliberately.

**Paths are defined once** in `services/common/paths.py`. Nothing computes its own
`Path(__file__).parents[N]` — that constant depends on how deep a file sits, so a
move silently changes it and nothing verifies the result. It has already caused two
defects.

**Security ships with the endpoint.** Auth, validation, scoped access and an audit
entry land in the same commit as the route they protect. Every object fetch goes
through a scoped accessor taking actor context; there is no bare primary-key lookup
in a route handler.

**Coordinates are never invented.** Every row in `data/seed/camera_geo.csv` traces to
a cached geocoder response or a named district centroid. A camera we cannot place
stays `unset` and is excluded from spatial queries *visibly*.
