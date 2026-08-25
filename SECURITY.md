# Security policy

Project SETU handles live law-enforcement video, vehicle registration data and personal
information belonging to members of the public. Security is treated as functional
requirement, not a hardening phase: an endpoint ships with authentication,
authorisation, input validation and an audit entry in the same commit that creates it.

## Reporting a vulnerability

Report privately to the address on the team's submission record. Do not open a public
issue. Include reproduction steps, affected component and, if known, impact. We will
acknowledge within 72 hours.

Do not test against `live.corp8.cloud`. It is third-party infrastructure operated by the
challenge organisers and is out of scope for any security testing.

## Controls enforced in CI

Every one of these fails the build; none is advisory.

| Control | Mechanism |
|---|---|
| No secrets in source or history | `gitleaks` over full history on every push |
| No known-vulnerable dependencies | `pip-audit --strict` against pinned requirements |
| Supply-chain transparency | CycloneDX SBOM generated and published per build |
| Reproducible infrastructure | Every compose image digest-pinned (`pin_digests.py --check`) |
| No declared-FPS timing | Exact-count assertion (`check_fps_guard.py`) |
| Type safety in services | `mypy --strict services` |

## Standing prohibitions

These are rejected in review regardless of context:

- Secrets, keys or connection strings in source, configuration or repository history.
  Everything comes from environment or the secret store.
- String-interpolated SQL. All queries parameterised.
- Any TLS bypass — `verify=False`, `rejectUnauthorized: false`,
  `NODE_TLS_REJECT_UNAUTHORIZED=0` — including in tests.
- `CORS: allow_origins=["*"]` together with credentialed requests.
- Authorisation decisions made in the frontend.
- Object identifiers taken from user input without an ownership or scope check.
- `pickle`, `eval`, `exec`, non-safe `yaml.load`, or shelling out with unsanitised input.
- Containers running as root, `latest` tags, or unpinned dependencies.
- `except Exception: pass`.
- Dependencies under AGPL, SSPL, BUSL or non-commercial licences.

## Credential handling

Upstream camera URLs and credentials never leave the adapter process. `open_stream()`
returns an opaque handle, never a URL. Credentials are never returned by an API, never
sent to the browser and never logged: `services/common/redact.py` installs a filter on
every log handler that masks URL userinfo, bare `rtsp://` URLs and known credential keys
before a record reaches any sink. The browser requests a stream by camera id; the
platform checks authorisation and mints a short-lived signed playback token.

## Data protection

- **Video stays with the owning department.** Metadata flows to the centre; full video is
  pulled centrally only on demand, for an alert or an authorised investigation, under audit.
- **Private cameras are consent-gated.** The private-CCTV adapter refuses to open a
  stream without a valid, unexpired `consent_ref` in the registry.
- **Audit is tamper-evident.** Entries are hash-chained
  (`entry_hash = SHA256(prev_hash || canonical_json(entry))`) with an endpoint that
  recomputes the chain and reports any break.
- **Evidence export requires a stated purpose**, recorded in the audit entry.

## Third-party feed etiquette

The evaluation gateway is infrastructure we do not own. We open only the cameras we are
actively processing, cap concurrent captures, pace reconnects with jittered exponential
backoff, and never publish to the gateway or call its control API.
