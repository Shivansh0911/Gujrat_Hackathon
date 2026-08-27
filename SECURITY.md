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
sent to the browser and never logged: `backend/services/common/redact.py` installs a filter on
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

## Dependency advisories

`pip-audit --strict` runs on every push against `backend/requirements.txt`. It fails
the build on any advisory in any pinned package, with **one** suppression, by ID.

### The one suppression

| Advisory | Package | Why it is suppressed |
|---|---|---|
| `PYSEC-2026-1325` / `CVE-2024-23342` | `ecdsa` 0.19.2 | A Minerva timing attack on the P-256 curve. **There is no fixed version:** upstream considers side-channel attacks out of scope and has stated there is no planned fix. |

**Why it does not apply to us.** `ecdsa` is present only as a transitive dependency of
`python-jose`. The advisory affects ECDSA signing, key generation and ECDH; signature
verification is explicitly unaffected. This platform issues **HS256** tokens
(symmetric HMAC) and signs evidence with **Ed25519** through `cryptography`. Nothing in
the tree calls an ECDSA signing, keygen or ECDH path, so the vulnerable code is not
reachable.

**Why it is suppressed by ID.** Dropping `--strict`, or excluding the package, would
silence every future advisory in it as well. Naming the single ID keeps the build
failing on anything new.

**The proper fix, not yet done.** Replace `python-jose` with `PyJWT`, which does not
depend on `ecdsa` at all. That removes the dependency rather than excusing it, and
`python-jose` is the less actively maintained of the two. It was not done in submission
week because it changes token issuance and verification, and the risk of touching
authentication outweighed removing an advisory we cannot reach. It is the first
security task afterwards.

### Keeping the pins current

The audit found **63 advisories across 7 packages** when it was first run against the
deployed pins on 2026-08-27 — `pypdf`, `starlette`, `cryptography`, `python-jose`,
`python-multipart`, `requests` and `ecdsa`. All but the unfixable one were resolved by
upgrading, and the full test suite and the nine deployment checks were re-run against
the upgraded stack before the pins were committed. Pinning is not the same as being
current; the audit is what makes the difference visible.
