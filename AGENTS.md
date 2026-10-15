# AGENTS

## Goal

FHIR Validation Service API built in Python using FastAPI, fronting the HL7
FHIR Validator CLI (`validator_cli.jar`).

## Architecture

```
+-------------------------------------------------------------------+
|                        Python FastAPI App                         |
|                     Endpoint: POST /fhir/$validate                |
|                     Endpoint: POST /fhir/$convert                 |
|                     Endpoint: GET  /fhir/$packages                |
|                     Endpoint: GET  /healthz                       |
+-------------------------------------------------------------------+
                                  |
   Parses Parameters resource (ig, profile, format, resource)
   -> app/fhir_parameters.py: parse_validate_parameters()
                                  |
                                  v
+-------------------------------------------------------------------+
|         ValidatorEngine (app/validator_engine.py)                 |
|  Owns a persistent subprocess:                                    |
|    java -jar validator_cli.jar server <port> -version <v> ...     |
|  Proxies over HTTP to that subprocess:                            |
|    POST /loadIG           (load new IGs into the running engine)  |
|    POST /validateResource  (validate; returns OperationOutcome)   |
|    POST /convert           (JSON<->XML format conversion)         |
+-------------------------------------------------------------------+
                                  |
     Caches IGs & deps    -> $HOME/.fhir/packages
     Caches terminology   -> $HOME/.fhir/validator-service/terminology-cache
     Resolves terminology -> tx.fhir.org (unless -tx overridden)
```

Key point: the validator jar's `server` subcommand starts a **long-lived
process that itself serves HTTP** (`/validateResource`, `/loadIG`, etc). The
FastAPI app never shells out per-request — it starts this subprocess once at
app startup (`app/main.py` lifespan) and proxies each `$validate` call to it
over HTTP via `httpx.AsyncClient`. New IGs referenced by a request are loaded
into the *already-running* engine via `POST /loadIG` rather than by
restarting the process, which is what makes IG loading dynamic without
paying the (multi-second-to-minutes) engine construction cost again.

If a validation or conversion request encounters a transport/protocol failure
(`ConnectError`, `ReadError`, `WriteError`, `CloseError`, or
`RemoteProtocolError`), the engine schedules one bounded in-process recovery.
The failed request is not replayed and retains the existing 502 response. The
engine enters preparation, drains in-flight engine calls, terminates the stale
child, recreates its HTTP client, clears the old process's loaded-IG bookkeeping,
and runs the normal startup/package-loading sequence again. Requests arriving
while recovery is in progress receive the existing transient 503 response.
HTTP error responses and timeout exceptions are not recovery triggers: a
validator-generated 4xx/5xx is a result, while a timeout can represent a slow
but healthy validation. Recovery attempts are bounded and exponentially backed
off; if all attempts fail, the engine remains unavailable until an operator or
the container orchestrator restarts it. `stop()` cancels any recovery task
during ASGI shutdown.

Full reference for the jar's HTTP server mode (verified against the real
`validator_cli.jar` during development):
https://confluence.hl7.org/spaces/FHIR/pages/441520076/Running+the+Validator+as+a+local+HTTP+service

## Request contract (`POST /fhir/$validate`)

Body is a FHIR `Parameters` resource, as **either JSON or XML** — the
envelope format is detected from the request's `Content-Type` header
(anything containing `xml` is parsed as FHIR XML via `app/fhir_xml.py`;
everything else is parsed as JSON via `app/fhir_parameters.py`). Both
parsers produce the same internal `ValidateRequest` shape:

- `ig` (0..*): IG package id/canonical, e.g. `"hl7.fhir.us.core#5.0.1"`
- `profile` (0..*): profile canonical URL to validate against
- `format` (0..1): media type of the resource being validated —
  `application/json`, `application/fhir+json`, `application/xml`, or
  `application/fhir+xml`. Ignored/irrelevant if the resource is embedded
  inline (see below), since an inline resource's representation always
  matches its envelope (JSON body -> JSON resource, XML body -> XML
  resource).
- `resource` (1..1, required): the resource to validate, provided as **one**
  of:
  - `parameter.resource` — an inline FHIR resource (JSON object or XML
    element, matching the envelope)
  - `parameter.valueString` — raw resource text, encoded per `format`
  - `parameter.valueBase64Binary` — raw resource bytes, encoded per `format`

Response representation is chosen via the request's `Accept` header
(`resolve_accept_format` in `app/fhir_parameters.py`); if absent/unrecognized
it defaults to the request envelope's own format (JSON in, JSON out; XML in,
XML out). This applies both to the upstream `OperationOutcome` and to
synthesized error outcomes we build ourselves (`app/main.py::_outcome_response`,
which uses `app/fhir_xml.py::operation_outcome_to_xml` for the XML case).

Note `app/fhir_xml.py` is a narrow, `$validate`-specific XML parser/serializer
(just `ig`/`profile`/`format`/`resource` extraction and a fixed-shape
OperationOutcome writer) — not a general FHIR XML<->JSON converter.

## `POST /fhir/$convert`

Converts a FHIR resource between its JSON and XML representations. Unlike
`$validate`, the body is the **raw resource itself** (no `Parameters`
wrapper) -- input format is detected from `Content-Type` the same way as
`$validate` (anything containing `xml` -> XML, else JSON).

Default behavior (no `Accept` header) is to **flip** the format -- JSON in
gives XML out and vice versa. This is a deliberate divergence from the
underlying validator engine's own `/convert`, which defaults to JSON output
regardless of input when `Accept` is omitted (verified against the real
jar); `app/main.py::convert` always computes and sends an explicit `Accept`
to the engine so the flip actually happens. An explicit `Accept:
application/fhir+json`/`application/fhir+xml` header overrides the flip
(e.g. to request a JSON->JSON round-trip/pretty-print), resolved the same
way as `$validate`'s response format (`resolve_accept_format`).

The upstream response (including its own conversion-error `OperationOutcome`
on bad input, HTTP 500) is passed through as-is; we only synthesize our own
error outcome for an empty request body or an unreachable/down engine.

## `GET /fhir/$packages`

Returns a plain JSON array (not a FHIR resource) describing the packages
listed in the `PACKAGES` env var (see table below), in the order given:

```json
[
  {"name": "hl7.fhir.us.core", "version": "5.0.1", "canonicalUrl": "hl7.fhir.us.core#5.0.1"}
]
```

Pure config reflection (`app/main.py::_package_summary`, splitting each
`<id>#<version>` string on the first `#`) — it doesn't touch the
`ValidatorEngine` at all, so it responds even if the engine isn't running.
It's the same list `ValidatorEngine.load_configured_packages()` (below)
ensures are fetched/cached/loaded at startup, so it should generally match
`GET /healthz`'s `loaded_igs` field — but the two are computed
independently, so a package that fails to load (see best-effort loading
below) would still show up here without actually being loaded.

## Configuring `PACKAGES` and `DEFAULT_IG` via `.env`

`PACKAGES`, `DEFAULT_IG`, and `CI_BUILD_REPOS` are read from `.env` (see
`.env.example` for a template) specifically so they can be **edited without
rebuilding the image** — for Docker deployments pass the file with `docker
run --env-file .env ...` (or mount it at `/app/.env`) rather than baking it
in. The Python-level defaults in `app/config.py` only serve as a fallback if
no `.env`/env var is provided. Verified: running the *same* built image with
vs. without `--env-file` produced different `loaded_igs` in `/healthz` —
confirming the values genuinely come from the runtime environment, not
something frozen into the image at build time.

At startup, `ValidatorEngine.load_configured_packages()` ensures every
package in `PACKAGES` plus `DEFAULT_IG` ends up loaded. Each one is tried
against, in order, a **3-tier fallback chain** (`ValidatorEngine._load_configured_package`),
stopping at the first tier that succeeds:

1. **The FHIR package registry**, via the same `POST /loadIG` used
   everywhere else (which itself resolves cache-first/network-fallback —
   inherent validator behavior, not something we write). This is tried
   for *every* package, including ones also present in `packages/` — the
   registry always gets first refusal.
2. **A direct download from HL7's CI build server**
   (`https://build.fhir.org/ig/<Org-or-User>/<Repo-Name>/package.tgz`, see
   `app/ci_build.py`) — only attempted if tier 1 failed *and* the version
   string looks like a draft/ci-build/screenshot build (`is_ci_build_version`
   — deliberately excludes "ballot": ballot versions are routinely
   published, see below) *and* the package has a matching entry in
   `CI_BUILD_REPOS` (`<packageId>#<version>=<Org-or-User>/<Repo-Name>`).
   We don't try to derive the GitHub org/repo from the package id — it's
   not mechanically derivable (e.g. `hl7.fhir.us.mdi` builds from
   `HL7/fhir-mdi-ig`, not `HL7/fhir-us-mdi` or `HL7/mdi` — read off the
   `url` field of that package's own `package.json`, or the `repo` field
   of the matching entry in `https://build.fhir.org/ig/qas.json`, to find
   it for a new package). A successful download is extracted straight into
   `$HOME/.fhir/packages/<packageId>#<version>`, then `/loadIG` is retried.
3. **The repo's local `packages/` folder** (see "Package cache preloading"
   below) — the true last resort, only reached if both network tiers
   failed (e.g. the package is genuinely unpublished *and* either has no
   `CI_BUILD_REPOS` entry or build.fhir.org is unreachable). The matching
   folder is copied into `$HOME/.fhir/packages` and `/loadIG` is retried
   one final time.

If every tier fails, the package is logged and skipped — loading is
best-effort, same as `load_all_cached_packages()` below; it never aborts
startup.

Loading `DEFAULT_IG` (or any package, at any tier) also recursively resolves
*its own* declared dependencies the same cache-first/network-fallback way,
per the FHIR Package Cache spec's recursive resolution rules — we don't
write any dependency-walking code ourselves for that part; it's inherent
validator behavior.

Note this is a deliberate change from just fetching everything through
`/loadIG` and letting a preload step race it: earlier versions of this
service copied every `packages/` entry into the cache *before* the engine
started, which made local files an unconditional cache hit — effectively
*first* priority for anything present there, not last. That's no longer how
it works: `packages/` is now only consulted per-package, and only after
both network tiers have already been tried and failed for that specific
package.

Verified against the real jar/registry: `packages/` contains exactly two
genuinely unpublished packages (`hl7.fhir.us.vdor#0.1.1-cibuild` and
`hl7.fhir.us.mdi#3.0.0-draft` -- confirmed absent from packages.fhir.org,
which only lists `hl7.fhir.us.mdi` up to `2.0.0-snapshot2`); the other 5
entries in the default `PACKAGES` list (`hl7.fhir.us.core#5.0.1`,
`hl7.fhir.us.mdi#2.0.0`, `hl7.fhir.us.bser#2.0.0-ballot`,
`hl7.fhir.us.vr-common-library#2.0.0`, `hl7.fhir.us.vrdr#3.0.0`) are all
genuinely published and are fetched over the network every time from a
fresh cache (tier 1, first try). The two unpublished ones both have a
`CI_BUILD_REPOS` entry in `.env.example` pointing at their real
build.fhir.org repo (`HL7/fhir-vdor`, `HL7/fhir-mdi-ig` — read off their
`package.json`'s own `url` field), so in an environment with internet
access they're expected to resolve at tier 2 rather than tier 3; `packages/`
only takes over if build.fhir.org itself is unreachable (e.g. an air-gapped
deployment). Don't assume "published" for any new package added to
`PACKAGES` without checking packages.fhir.org first (see mdi#3.0.0-draft
above for why that assumption already broke once).

## Development commands

```bash
# one-time setup (editable install incl. dev deps)
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# run the full test suite (all validator interactions are mocked — no
# Java/jar required)
.venv/bin/pytest -q

# run a single test file / test
.venv/bin/pytest tests/test_fhir_parameters.py -q
.venv/bin/pytest tests/test_validate_endpoint.py::test_validate_happy_path_returns_upstream_operation_outcome -q

# lint + format (must both be clean before committing)
.venv/bin/ruff check .
.venv/bin/ruff format .

# run the API locally against a real validator_cli.jar
curl -sL -o /tmp/validator_cli.jar \
  https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar
VALIDATOR_JAR_PATH=/tmp/validator_cli.jar .venv/bin/uvicorn app.main:app --reload
```

There is no separate typecheck/codegen step. `ruff check` + `pytest` is the
whole gate; run both before considering a change done.

> **Common pitfall:** running `uvicorn app.main:app` directly on the host
> (i.e. not via the Dockerfile) with no `VALIDATOR_JAR_PATH` set will fail
> with `Unable to access jarfile /opt/validator/validator_cli.jar` — that
> default path only exists inside the built Docker image. Always set
> `VALIDATOR_JAR_PATH` to a real downloaded jar (or `AUTO_START_VALIDATOR=false`)
> for local/host runs.

### Docker

```bash
docker build -t fhir-validator-service .
docker run -p 8080:8080 -v fhir-cache:/root/.fhir fhir-validator-service
```

The Dockerfile downloads the **latest** `validator_cli.jar` from GitHub at
build time (HL7 does not guarantee old versions keep working — see the
"Limitations of Use" section of the confluence docs — pin a release tag in
the Dockerfile if you need reproducible builds). Base image is
`python:3.12-slim` (Debian trixie), which requires
`openjdk-21-jre-headless` — `openjdk-17-jre-headless` is **not installable**
on trixie, don't revert to it.

The `/root/.fhir` volume is what makes the package/terminology cache survive
container restarts — always mount it in any real deployment, or every
restart re-downloads every IG and re-warms the terminology cache from
scratch (first boot took ~10s locally just for the base R4 package; adding
IGs adds more).

## Package cache preloading (`app/package_cache.py`)

Per the [FHIR Package Cache spec](https://confluence.hl7.org/display/FHIR/FHIR+Package+Cache),
a package is "installed" purely by the existence of a
`<packageId>#<version>/package/...` folder under the cache root
(`$HOME/.fhir/packages` — no separate manifest/registration step). That
means preloading a package is just: put its already-extracted folder there
before it's needed, so it never triggers a network fetch.

`packages/` (default: `Settings.packages_dir`, relative to cwd) holds
already-extracted `<packageId>#<version>/package/...` folders for **local
IGs** — packages not published on the FHIR package registry (e.g.
draft/ci-build versions) and not (yet, or ever) mirrored by HL7's CI build
server either. Repo layout expected:

```
packages/
  hl7.fhir.us.vdor#0.1.1-cibuild/
    package/
      package.json
      StructureDefinition-....json
      ...
  hl7.fhir.uv.ips#2.0.0/
    package/
      ...
```

This is exactly the same folder shape as `$HOME/.fhir/packages` itself — you
can drop in a package by copying it straight from an existing cache, or by
extracting a `package.tgz` (`tar xzf package.tgz` into a dir named
`<id>#<version>`, with the `.tgz`'s `package/` folder as-is).

`app/package_cache.py` exposes two copy functions, both **skipping any
package that's already cached** (never overwrite/refresh an existing
entry — this is a one-time seed, not a sync):

- `preload_package(name, source_dir, cache_dir)` — copies a single named
  package. Used in two places: 1) `ValidatorEngine.start()`, for each
  `STARTUP_IGS` entry, *before* the validator subprocess is spawned — those
  are passed straight to the jar as `-ig` CLI args (see `_build_command`),
  which the jar resolves itself the instant it starts, with no chance for
  us to intervene afterwards, so any local-only one among them must already
  be on disk. 2) `ValidatorEngine._load_configured_package()`, as the
  **last-resort tier** of the `PACKAGES`/`DEFAULT_IG` fallback chain (see
  "Configuring PACKAGES and DEFAULT_IG via .env" above) — only reached
  after both the FHIR package registry and the build.fhir.org direct
  download have already been tried and failed for that specific package.
- `preload_packages(source_dir, cache_dir)` — the bulk (whole-directory)
  variant, kept for callers that genuinely want everything in a folder
  seeded at once; nothing in `ValidatorEngine` calls this anymore (see
  below for why), but it's still exercised directly by
  `tests/test_package_cache.py`.

Note the deliberate change from earlier versions of this service: previously
`ValidatorEngine.start()` called `preload_packages()` unconditionally on
**every** `packages/` entry before the engine even launched, which made
local files an unconditional cache hit for anything present there —
effectively *first* priority, not last. That's no longer how `PACKAGES`/
`DEFAULT_IG` work (see above); `packages/` is now consulted per-package, on
demand, only after both network tiers have already failed for that specific
package. `STARTUP_IGS` is the one remaining place that still preloads
proactively, and only for the specific IGs it actually references — never
the rest of `packages/`.

The Dockerfile does `COPY packages ./packages` (before `pip install`), so
these are baked into the image regardless — a fresh container still never
needs an actual `packages/`-repo checkout on the host to have the fallback
tier available, it's just consulted lazily now rather than eagerly.

## Loading cached packages into the engine at startup

Preloading (above) only puts packages on *disk*; it doesn't load them into
the *running engine's* memory -- that still happened lazily, only when a
request's `ig` parameter referenced one (via `ensure_igs_loaded()` ->

`POST /loadIG`).

`ValidatorEngine.load_all_cached_packages()` closes that gap: after the
engine reports ready, it scans the **entire** actual FHIR package cache
(`~/.fhir/packages` -- not just `packages_dir`, everything found there,
including whatever was already cached from prior runs/unrelated activity)
and `POST /loadIG`s every package not already loaded. Controlled by
`LOAD_CACHED_PACKAGES_ON_STARTUP` (default `true`).

This is deliberately **best-effort, not fail-fast**: `~/.fhir/packages` is a
shared, long-lived directory that can accumulate packages from unrelated
prior activity -- multiple FHIR versions, multiple versions of the same IG,
etc. (verified on a real dev machine: a cache with both `hl7.fhir.r4.core`
and `hl7.fhir.r5.core`, plus several versions each of `hl7.terminology.r4`
and `hl7.fhir.us.core`). A failure loading any one package is logged and
skipped; it does not raise, abort startup, or block the others. Verified
against the real jar with a 36-package "dirty" cache like this -- all 37
(36 + 1 newly preloaded) loaded successfully in ~49s with zero failures,
but the code does not assume that will always be true.

**Test hygiene note:** `tests/test_validator_engine.py`'s `_engine_with_command()`
helper defaults `load_cached_packages_on_startup=False`, `packages`/`default_ig`
to empty strings, and `packages_dir` to a nonexistent path, specifically so
subprocess-lifecycle tests never touch this machine's real `~/.fhir/packages`
or the repo's own `packages/` folder, or make an unexpected `/loadIG` call
using the real `PACKAGES` default. If you add a new test that calls
`engine.start()` directly (bypassing this helper), carry that same isolation
forward -- don't let a test's default settings resolve to real paths. It
also defaults `validate_initial_load_resource_on_startup=False` (see
"Startup warm-up validation" below) for the same reason -- the real default
resolves `initial_load_resource_path` against the repo's own
`initial_load_resource.json` and calls `validate_resource()`, which isn't
meaningful against the trivial `sh`/`sleep` stand-in commands these tests
spawn.

## Startup warm-up validation

After `default_ig` (and its dependencies) finish loading,
`ValidatorEngine.validate_initial_load_resource()` validates a single
sample resource -- `INITIAL_LOAD_RESOURCE_PATH` (default:
`initial_load_resource.json`, a resource checked into the repo root) --
against the now-fully-loaded engine, and logs the outcome (issue count, or
the upstream `OperationOutcome` body on an HTTP error). This is purely
diagnostic: it exercises the full validate path once at startup so a
`default_ig`/sample-resource mismatch (e.g. a profile the IG doesn't
actually define) shows up in the logs immediately rather than on the first
real request, but it never raises or blocks startup regardless of the
outcome.

It's skipped entirely (a no-op, not an error) if `default_ig` is empty, the
configured `INITIAL_LOAD_RESOURCE_PATH` doesn't exist, or
`VALIDATE_INITIAL_LOAD_RESOURCE_ON_STARTUP=false`. The resource's own
representation (JSON/XML) is inferred from the path's file extension
(`.xml` -> XML, anything else -> JSON) and sent with no explicit `profile`
query params -- it relies entirely on the resource's own `meta.profile`
entries (which is what actually ties it to `default_ig`), the same as a
real `$validate` request that omits the `profile` parameter.

## Environment variables (`app/config.py`)

| Var | Default | Notes |
|---|---|---|
| `VALIDATOR_JAR_PATH` | `/opt/validator/validator_cli.jar` | path to the jar |
| `VALIDATOR_HOST` / `VALIDATOR_PORT` | `127.0.0.1` / `8081` | internal engine HTTP address, not the public API port |
| `AUTO_START_VALIDATOR` | `true` | set `false` for local dev/tests without Java/the jar |
| `FHIR_VERSION` | `4.0` | passed as `-version` when the engine starts |
| `STARTUP_IGS` | `` (empty) | **comma-separated**, e.g. `hl7.fhir.us.core#5.0.1,hl7.fhir.uv.ips` — NOT JSON, despite pydantic-settings' usual list-from-env convention (see comment in config.py for why) |
| `TERMINOLOGY_SERVER` | unset (-> tx.fhir.org) | passed as `-tx <url>` |
| `TERMINOLOGY_CACHE_DIR` | `$HOME/.fhir/validator-service/terminology-cache` | explicit directory passed as `-txCache`; use a separate directory per validator implementation/terminology endpoint, or `n/a` to disable terminology caching |
| `CLEAR_TERMINOLOGY_CACHE_ON_STARTUP` | `false` | pass `-clear-tx-cache` on the next validator startup; useful for manually recovering from stale terminology-server session state |
| `SNOMED_EDITION` | `us` | passed as `-sct <edition>`; valid choices per the validator's own `-sct` option: `intl \| us \| uk \| au \| nl \| ca \| se \| dk \| es`. Unset/empty falls back to the validator's own default (`intl`) |
| `VALIDATOR_EXTRA_ARGS` | `` (empty) | comma-separated raw CLI args appended to `server ...` |
| `VALIDATOR_STARTUP_TIMEOUT_SECONDS` | `300` | cold start with big IGs can take minutes |
| `VALIDATOR_REQUEST_TIMEOUT_SECONDS` | `120` | per-request httpx timeout to the engine |
| `VALIDATOR_KEEPALIVE_INTERVAL_SECONDS` | `300` | interval in seconds for validating `INITIAL_LOAD_RESOURCE_PATH` to keep the terminology-server cache/session active; `0` or a negative value disables it |
| `AUTO_RECOVER_VALIDATOR` | `true` | schedule an in-process restart after a transport/protocol failure; the failed request is not replayed |
| `VALIDATOR_RECOVERY_MAX_ATTEMPTS` | `3` | maximum consecutive restart attempts for one recovery event |
| `VALIDATOR_RECOVERY_BACKOFF_SECONDS` | `5` | delay before the second recovery attempt; subsequent delays are multiplied by `VALIDATOR_RECOVERY_BACKOFF_MULTIPLIER` |
| `VALIDATOR_RECOVERY_BACKOFF_MULTIPLIER` | `2` | exponential multiplier for recovery retry delays |
| `VALIDATOR_RECOVERY_OPERATION_DRAIN_TIMEOUT_SECONDS` | `10` | maximum time to wait for in-flight engine calls before forcing the child restart |
| `PACKAGES_DIR` | `packages` | dir of pre-extracted packages copied into `$HOME/.fhir/packages` on startup, see "Package cache preloading" above; missing dir is a no-op |
| `LOAD_CACHED_PACKAGES_ON_STARTUP` | `true` | load every package found in `$HOME/.fhir/packages` into the running engine at startup, see "Loading cached packages into the engine at startup" above; best-effort, set `false` for purely lazy/on-demand loading |
| `PACKAGES` | see `app/config.py` | comma-separated `<id>#<version>` list; canonical source is `.env` (see `.env.example`), not this Python-level fallback -- returned by `GET /fhir/$packages` *and* fetched/cached/loaded into the engine at startup, see "Configuring PACKAGES and DEFAULT_IG via .env" above |
| `DEFAULT_IG` | `` (empty) | primary `<id>#<version>` IG for this deployment; loaded (with dependencies auto-resolved) alongside `PACKAGES` at startup, see above; canonical source is also `.env` |
| `INITIAL_LOAD_RESOURCE_PATH` | `initial_load_resource.json` | path to a sample resource validated at startup and periodically against `DEFAULT_IG`, see "Startup warm-up validation" above; missing file is a no-op |
| `VALIDATE_INITIAL_LOAD_RESOURCE_ON_STARTUP` | `true` | set `false` to skip the startup warm-up validation above |
| `CI_BUILD_REPOS` | `` (empty) | comma-separated `<id>#<version>=<Org-or-User>/<Repo-Name>` mapping used as tier 2 of the `PACKAGES`/`DEFAULT_IG` fallback chain, see "Configuring PACKAGES and DEFAULT_IG via .env" above; canonical source is also `.env` |
| `CORS_ALLOW_ORIGINS` | `*` | comma-separated list of allowed CORS origins; also makes every route respond to CORS preflight `OPTIONS` requests via Starlette's `CORSMiddleware` (see `app/main.py`) |
| `CUSTOM_PATH` | `` (empty) | external path prefix this service is mounted under by a reverse proxy (e.g. `/my-service`), applied via FastAPI's `root_path` (https://fastapi.tiangolo.com/advanced/behind-a-proxy/) rather than baked into any route's registered path -- so it works whether the proxy strips the prefix before forwarding (container only ever sees `/fhir/docs`) or forwards the full path unstripped (container sees `/my-service/fhir/docs` -- Starlette's routing strips a leading `root_path` match off the incoming path itself, see `get_route_path`), and either way it's purely informational for URL *generation*: it makes the docs/redoc HTML's self-referencing `openapi_url` (fixing "Failed to load API definition"/"Fetch error Not Found /fhir/openapi.json" in Swagger UI) and the OpenAPI schema's `servers` entry (fixing "Try it out"/"Execute", and any client generated from the spec, for the actual operations -- `/fhir/$validate` etc, which like every other route are never prefixed at the route level) come out correctly prefixed. Leading/trailing slashes are normalized; empty (default) leaves both alone. |
| `LOG_LEVEL` | `DEBUG` | application logging level passed to `logging.basicConfig`; override with `INFO`, `WARNING`, or another supported Python logging level for quieter deployments |

The public FastAPI port is set via the ASGI server invocation (`uvicorn
app.main:app --port ...`), not an env var.

The validator's terminology cache is deliberately separate from the shared
FHIR package cache. If a validation response reports that a terminology cache
was never issued by `tx.fhir.org`, or that a UUID cache expired after being
idle, the service marks the terminology cache stale and performs one bounded
recovery restart with `-clear-tx-cache`. Detection is based on the specific
diagnostic text, not the upstream HTTP status, because the validator can return
the diagnostic inside an otherwise normal HTTP 200 `OperationOutcome`.
The failed request is not replayed. This handles stale server-issued cache IDs
left by a crashed, idle, or different validator process; ordinary validator
HTTP errors and request timeouts do not clear the cache.

When `DEFAULT_IG` and `INITIAL_LOAD_RESOURCE_PATH` are configured,
`VALIDATOR_KEEPALIVE_INTERVAL_SECONDS` runs the same validation periodically
(five minutes by default) to keep the terminology-server session active. The
setting can be `0` or negative to disable the background validation.

## Known limitations (intentional, not bugs)

- `ig`/`profile` values only accept `valueString`/`valueUri`/`valueCanonical`/
  `valueCode`/`valueUrl` — not FHIR datatypes requiring deeper parsing.
- `resolve_accept_format` only matches exact media-type aliases from the
  `Accept` header (after stripping `;q=...`); it does not implement full
  HTTP content negotiation (multiple weighted candidates, wildcards beyond
  `*/*`).
- The `ValidatorEngine` runs a single validation engine per process (one
  FHIR version, one set of loaded IGs, shared across all requests). There is
  no per-request isolation; concurrent requests share loaded IGs, which is
  the entire point of the persistent-engine design (see Architecture above)
  but means you can't validate against conflicting IG *versions*
  simultaneously without loading both into the same engine.

## Testing notes

- `tests/conftest.py` provides a `FakeValidatorEngine` swapped onto
  `app.state.validator_engine` after FastAPI's lifespan startup runs (with
  `AUTO_START_VALIDATOR=false`, so no real subprocess is spawned in tests).
- `tests/test_validator_engine.py` covers `ValidatorEngine`'s subprocess
  lifecycle (readiness detection, fail-fast on early process exit, timeout)
  using trivial `sh`/`sleep` commands — no Java or the real jar needed for
  these either, just generic subprocess plumbing.
- `tests/test_package_cache.py` covers `preload_packages()` against `tmp_path`
  fixtures — also no Java/jar needed.
- `ValidatorEngine.start()` fails fast (not after waiting out
  `VALIDATOR_STARTUP_TIMEOUT_SECONDS`) if the subprocess exits before
  reporting readiness, and includes the process's own captured output (e.g.
  the "Unable to access jarfile" message above) in the raised
  `ValidatorEngineError`. If you touch the startup-wait logic in
  `app/validator_engine.py`, keep this property — it used to silently hang
  for the full timeout on any startup failure before this was fixed.
- If you need to test against the *real* jar (e.g. verifying a new HTTP
  contract assumption), download it fresh — don't commit it — and point
  `VALIDATOR_JAR_PATH` at it; `ValidatorEngine.start()`/`.stop()` manage the
  subprocess lifecycle directly and were manually verified this way during
  development (real engine start -> `/loadIG` -> `/validateResource` ->
  clean shutdown, and the same flow through the built Docker image).
