# FHIR Validation Service

A Python FastAPI service that fronts the HL7 FHIR Validator CLI (`validator_cli.jar`) to provide RESTful endpoints for validating and converting FHIR resources.

## Project Purpose and Features

This service provides a REST API for:
- Validating FHIR resources against Implementation Guides (IGs) and profiles
- Converting FHIR resources between JSON and XML representations
- Managing FHIR package dependencies
- Health monitoring of the validation engine

### Key Features
- FastAPI-based REST API
- Persistent HL7 FHIR validator engine for performance
- Support for both JSON and XML FHIR formats
- Dynamic IG loading without restarting the engine
- Automatic recovery from engine failures
- Periodic terminology-cache keepalive validation
- Configuration-driven package management
- Docker-ready deployment

## Architecture

The service consists of:
- A Python FastAPI application providing REST endpoints
- A persistent Java subprocess running the HL7 `validator_cli.jar` in server mode
- HTTP proxying between FastAPI and the validator engine

```
+------------------+    HTTP    +----------------------------------+
|   FastAPI App    | <-------> |  Validator Engine (validator_cli.jar)
|                  |            |  - Loads IGs                    |
|  - Parses FHIR   |            |  - Validates resources          |
|    Parameters    |            |  - Converts formats             |
|  - Proxies calls |            |  - Manages packages             |
+------------------+            +----------------------------------+
```

## API Endpoints

### POST /fhir/$validate
Validates a FHIR resource against specified IGs and profiles.

**Request Body**: FHIR `Parameters` resource (JSON or XML)
- `ig`: Implementation Guide package identifiers
- `profile`: Profile canonical URLs to validate against
- `format`: Media type of the resource being validated
- `resource`: The FHIR resource to validate, provided as one of:
  - `parameter.resource`: Inline FHIR resource (representation matches envelope format)
  - `parameter.valueString`: Raw resource text
  - `parameter.valueBase64Binary`: Raw resource bytes

**Response**: OperationOutcome resource (JSON or XML)

#### Example curl requests:
```bash
# JSON $validate with inline Patient resource
curl -X POST "http://localhost:8080/fhir/\$validate" \
  -H "Content-Type: application/json" \
  -H "Accept: application/fhir+json" \
  -d '{"resourceType":"Parameters","parameter":[{"name":"ig","valueString":"hl7.fhir.us.core#5.0.1"},{"name":"profile","valueString":"http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"},{"name":"resource","resource":{"resourceType":"Patient","id":"example"}}]}'

# XML $validate
curl -X POST "http://localhost:8080/fhir/\$validate" \
  -H "Content-Type: application/xml" \
  -H "Accept: application/fhir+xml" \
  -d '<?xml version="1.0" encoding="UTF-8"?><Parameters xmlns="http://hl7.org/fhir"><parameter><name value="ig"/><valueString value="hl7.fhir.us.core#5.0.1"/></parameter><parameter><name value="profile"/><valueString value="http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient"/></parameter><parameter><name value="resource"/><resource><Patient xmlns="http://hl7.org/fhir"><id value="example"/></Patient></resource></parameter></Parameters>'

# XML $convert
curl -X POST "http://localhost:8080/fhir/\$convert" \
  -H "Content-Type: application/xml" \
  -H "Accept: application/fhir+json" \
  -d '<Patient xmlns="http://hl7.org/fhir"><id value="example"/></Patient>'

# JSON->XML $convert
curl -X POST "http://localhost:8080/fhir/\$convert" \
  -H "Content-Type: application/json" \
  -H "Accept: application/fhir+xml" \
  -d '{"resourceType":"Patient","id":"example"}'
```

### POST /fhir/$convert
Converts a FHIR resource between JSON and XML representations.

**Request Body**: Raw FHIR resource (JSON or XML, no Parameters wrapper)
- Unlike $validate, the body is the raw resource itself, not wrapped in a Parameters resource

**Response**: Converted FHIR resource in the requested format
- When Accept header is omitted, the format flips (JSON in → XML out, XML in → JSON out)
- Explicit Accept header overrides the flip behavior

### GET /fhir/$packages
Returns a list of configured FHIR packages.

**Response**: JSON array of package descriptions

### GET /healthz and /fhir/health
Health check endpoints for the service and validator engine.

**Response**: Service status and loaded IGs information

### Terminology cache keepalive

When `DEFAULT_IG` and `INITIAL_LOAD_RESOURCE_PATH` are configured, the service
validates that resource every five minutes by default. This keeps the
terminology-server cache/session active and reduces avoidable validator
restarts. Set `VALIDATOR_KEEPALIVE_INTERVAL_SECONDS=0` to disable it.

## Response and Accept Header Behavior

- Response format is determined by the `Accept` header
- If no `Accept` header is present, defaults to the request envelope format
- Supported formats: `application/json`, `application/fhir+json`, `application/xml`, `application/fhir+xml`

## Prerequisites

- Python 3.11+
- Java Runtime (for validator_cli.jar)
- Docker (for containerized deployment)

## Local Setup

### Development Environment

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run tests (without Java/validator)
pytest -q

# Run API locally without validator
AUTO_START_VALIDATOR=false uvicorn app.main:app --reload
```

### Running with Real Validator

```bash
# Download validator_cli.jar
curl -sL -o /tmp/validator_cli.jar \
  https://github.com/hapifhir/org.hl7.fhir.core/releases/latest/download/validator_cli.jar

# Run with real validator
VALIDATOR_JAR_PATH=/tmp/validator_cli.jar uvicorn app.main:app --reload
```

## Docker Deployment

```bash
# Build image
docker build -t fhir-validator-service .

# Run container with persistent cache
docker run -p 8080:8080 -v fhir-cache:/root/.fhir fhir-validator-service

# Run with custom environment variables
docker run -p 8080:8080 --env-file .env -v fhir-cache:/root/.fhir fhir-validator-service
```

### Docker Volume
The `/root/.fhir` volume is essential for persisting:
- FHIR package cache
- Terminology cache

Without this volume, every container restart will re-download all packages.

## Configuration

Configuration is managed through environment variables and `.env` files.

### Important Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PACKAGES` | (see config.py) | Comma-separated list of `<id>#<version>` packages |
| `DEFAULT_IG` | (empty) | Primary IG for this deployment |
| `STARTUP_IGS` | (empty) | Comma-separated IGs to preload at startup |
| `CI_BUILD_REPOS` | (empty) | Mapping of packages to CI build repositories |
| `VALIDATOR_JAR_PATH` | `/opt/validator/validator_cli.jar` | Path to validator jar |
| `AUTO_START_VALIDATOR` | `true` | Whether to start validator at boot |
| `TERMINOLOGY_SERVER` | (validator default) | Terminology server URL, usually `https://tx.fhir.org` |
| `TERMINOLOGY_CACHE_DIR` | `$HOME/.fhir/validator-service/terminology-cache` | Isolated terminology cache/session directory |
| `CLEAR_TERMINOLOGY_CACHE_ON_STARTUP` | `false` | Clear stale terminology state on the next validator startup |
| `LOAD_CACHED_PACKAGES_ON_STARTUP` | `true` | Load all cached packages at startup |
| `LOG_LEVEL` | `DEBUG` | Application logging level; override with `INFO`, `WARNING`, or another Python logging level |

### Recovery Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTO_RECOVER_VALIDATOR` | `true` | Enable automatic recovery |
| `VALIDATOR_RECOVERY_MAX_ATTEMPTS` | `3` | Maximum recovery attempts |
| `VALIDATOR_RECOVERY_BACKOFF_SECONDS` | `5` | Initial backoff delay |
| `VALIDATOR_RECOVERY_BACKOFF_MULTIPLIER` | `2` | Backoff multiplier |
| `VALIDATOR_RECOVERY_OPERATION_DRAIN_TIMEOUT_SECONDS` | `10` | Maximum time to wait for in-flight engine calls before forcing restart |

## Package Cache and Local Packages

The service supports preloading local packages from the `packages/` directory:

```
packages/
├── hl7.fhir.us.vdor#0.1.1-cibuild/
│   └── package/
└── hl7.fhir.uv.ips#2.0.0/
    └── package/
```

Local packages are used as a last resort when:
1. Package is not available on the FHIR package registry
2. Package is not available via CI build servers
3. Network is unavailable

Packages are copied to `$HOME/.fhir/packages/` on-demand.

## Automatic Recovery

The service includes automatic recovery mechanisms:

1. Detects transport/protocol failures to the validator engine
2. Schedules bounded in-process recovery
3. Drains in-flight requests before restarting
4. Clears old process bookkeeping
5. Restarts with exponential backoff
6. Responds with 503 during recovery

Recovery is bounded and will not retry indefinitely.

## Limitations

- Single validation engine per process (shared across requests)
- No per-request isolation between different IG versions
- `ig`/`profile` values only accept simple FHIR datatypes
- Limited HTTP content negotiation
- Recovery does not replay failed requests

## Development Commands

```bash
# Run full test suite
pytest -q

# Run specific test
pytest tests/test_fhir_parameters.py -q
pytest tests/test_validate_endpoint.py::test_validate_happy_path_returns_upstream_operation_outcome -q

# Lint and format
ruff check .
ruff format .
```

## Operational Notes

1. The validator jar is downloaded at Docker build time from GitHub releases
2. HL7 does not guarantee old versions will continue working
3. To pin a specific version, modify the Dockerfile to use a release tag
4. The service can run without the validator for development/testing
5. Health endpoints can be used for monitoring and orchestration

If validation reports that a terminology cache was never issued by `tx.fhir.org`
or that a UUID cache expired after being idle, the service automatically
schedules one recovery restart with `-clear-tx-cache`. Detection is based on
the diagnostic text rather than HTTP status because the validator may return
the message inside an HTTP 200 `OperationOutcome`. This repairs stale
server-issued cache IDs after a crash, idle timeout, or shared cache state. The
failed request is not replayed; subsequent requests are gated while recovery
runs.
