# apinfer — CLAUDE.md

## What this project is

apinfer is a passive API schema observation tool for Python backend applications.
Its purpose: infer the real-world contract of any REST API by observing live HTTP traffic,
detect when that contract changes over time, and surface those changes before they break
consumers.

The fundamental thesis: **the running application is the ground truth, not the documentation file.**

---

## The problem being solved

API schemas drift silently. A field changes type, becomes optional, disappears in edge
cases, or appears in responses that aren't documented. Existing tools (Schemathesis,
oasdiff, Prism) assume the OpenAPI spec is the source of truth. apinfer inverts this:
it learns the schema from real traffic and alerts when the observed contract changes.

This matters because:
- Teams with no OpenAPI spec have no tooling at all
- Teams with a spec often have a spec that's already wrong (generated incorrectly, or
  drifted from the implementation after a refactor)
- No tool currently infers schema from traffic and tracks it over time as the primary
  workflow — only as a secondary verification step

---

## Two modes — build Spec-free first

### Mode 1: Spec-free (MVP — implement this first)

No spec required. apinfer observes traffic, constructs its own schema, and then
compares that schema against its own historical snapshots. Any deviation from the
previously-observed contract is flagged as drift. Works for every Python project
regardless of documentation status.

### Mode 2: Spec-aware (second phase)

If an OpenAPI spec file is provided, apinfer additionally compares the inferred
schema against it. This surfaces discrepancies between what the spec claims and what
the API actually does. This mode is a strict superset of Spec-free — the entire traffic
observation pipeline is identical. Only the comparison target changes.

**The shared pipeline is the investment. Build it once.**

---

## Architecture: five layers, bottom-up data flow

```
[ CLI · Web report · pytest plugin ]   ← output layer
             ↑
       [ Drift detector ]               ← analysis layer
             ↑                ↖ OpenAPI spec (optional, Mode 2)
 [ Schema inferrer | Storage ]         ← core engine
             ↑
  [ Request/Response recorder ]        ← capture layer
             ↑
  [ ASGI middleware | WSGI middleware ] ← transport layer
             ↑
       [ Python application ]          ← user's app
```

### Transport layer
Thin middleware that intercepts every request/response pair. Has zero knowledge of
schema or drift — its only job is to extract the HTTP exchange and hand it to the
recorder. Two variants: ASGI (FastAPI, Starlette, Litestar) and WSGI (Django, Flask,
Falcon). The core pipeline must not know which framework is running.

### Capture layer
Normalizes the intercepted exchange into a framework-agnostic `CapturedExchange`
dataclass. Uses reservoir sampling to maintain a fixed-size buffer — memory overhead
is constant regardless of traffic volume. Processing is deferred off the request path.

### Core engine — Schema inferrer
Given a set of JSON payloads for a specific endpoint, determines: each field's type(s),
presence rate (how often it appears across N samples), nullability, and whether any
field is polymorphic. Fields present in less than 100% of samples are marked optional.
Output is a standard JSON Schema object.

### Core engine — Schema storage
Persists versioned snapshots to a local SQLite database (`apinfer.db`). Each
snapshot is keyed by: endpoint path pattern, HTTP method, status code, and a
content hash. Stores timestamp and sample count alongside the schema.

### Analysis layer — Drift detector
Compares the latest inferred schema against the previous snapshot for the same
endpoint. Classifies changes by severity:
- **Breaking**: field type changed, required field disappeared, status code removed
- **Non-breaking**: new optional field appeared, new status code added
- **Informational**: field optionality changed, field appeared in fewer samples

### Output layer
Three consumers of drift reports:
- `apinfer report` — rich terminal output, human-readable
- `apinfer diff` — shows what changed between two snapshots
- Web report — static HTML with schema timeline (generated, not a running server)
- pytest plugin — fails the test suite if unexpected breaking drift is detected

---

## Core concepts

**CapturedExchange**: The normalized, framework-agnostic unit of data flowing through
the pipeline. Contains: method, path pattern (with path params abstracted), status code,
request body (if any), response body, and timestamp.

**Schema snapshot**: A point-in-time JSON Schema representation of a specific
endpoint's response structure, derived from N captured exchanges. Tagged with
content hash, timestamp, sample count, and endpoint key.

**Drift report**: The output of comparing two snapshots. Contains a list of changes
with severity, the before/after values, and whether the change is expected (if the user
has explicitly acknowledged it).

**Reservoir sampling**: A fixed-size sample buffer that maintains statistical
representativeness regardless of total traffic volume. When the buffer is full, new
samples replace existing ones with a probability that keeps the distribution uniform.

---

## Scope — what apinfer is NOT

- Not a load balancer or reverse proxy
- Not a security scanner
- Not a test data generator
- Not a performance monitoring tool
- Not a mock server
- Not a real-time alerting system (it's a developer tool, not ops infrastructure)

---

## Key design constraints

**Zero required configuration**: `pip install apinfer` plus one middleware line
must produce useful output. No YAML config, no Docker, no external services for
the core experience.

**Non-invasive**: Middleware must not materially affect response latency. Everything
that isn't strictly on the request/response path must be deferred.

**Self-contained storage**: Default is a single SQLite file in the project directory.
No database server. No cloud dependency.

**Framework-agnostic core**: `core/` must import nothing from web frameworks.
ASGI and WSGI adapters are in `transport/` and normalize everything into
`CapturedExchange` before touching the core.

**Python 3.11+**: Use `dataclasses`, `pathlib`, `tomllib`, `match` statements,
`TypeAlias`. Avoid patterns that require older Python compatibility shims.

---

## Repository layout

```
apinfer/
  core/
    models.py          # CapturedExchange, Snapshot, DriftReport dataclasses
    capture.py         # Reservoir sampler + buffer management
    inferrer.py        # JSON schema inference from a set of payloads
    storage.py         # SQLite read/write for schema snapshots
    detector.py        # Drift detection: compare two Snapshot objects
  transport/
    asgi.py            # ASGI middleware → CapturedExchange
    wsgi.py            # WSGI middleware → CapturedExchange
  output/
    cli.py             # Typer CLI (apinfer report, diff, status)
    report.py          # Static HTML report generator
    pytest_plugin.py   # pytest plugin (conftest entrypoint hook)
  config.py            # Config loader: env vars + pyproject.toml [tool.apinfer]
  __init__.py          # Public API: ApinferMiddleware, ApinferASGI
```

---

## Build order

Start here, in this order:

1. **`core/models.py`** — define all dataclasses first. Nothing else can be built
   without knowing the shape of the data.

2. **`core/inferrer.py`** — get schema inference working against static test fixtures
   before touching any web framework. The inferrer is the hardest algorithmic piece.

3. **`core/storage.py`** — persist and retrieve snapshots. Validate round-trips.

4. **`core/detector.py`** — compare two snapshots and produce a DriftReport.

5. **`transport/asgi.py`** — ASGI middleware first (FastAPI is the primary target).

6. **`output/cli.py`** — basic `apinfer report` and `apinfer diff` commands.

7. **`transport/wsgi.py`** — WSGI adapter (same core, different surface).

8. **`output/pytest_plugin.py`** — CI integration.

9. **Spec-aware mode** — add OpenAPI comparison only after Spec-free is solid end-to-end.

---

## What "done" looks like for MVP

A developer can:

```bash
pip install "apinfer[asgi]"
```

Add one line to their FastAPI app:

```python
from apinfer import ApinferMiddleware
app.add_middleware(ApinferMiddleware)
```

Run the app, make some requests, then:

```bash
apinfer report      # shows inferred schema for all endpoints
apinfer diff        # shows what changed since last snapshot
```

That is the complete MVP. No dashboard, no alerting, no cloud. Just local schema
observation that works on the first try.

---

## The bigger picture (context for future decisions)

The long-term product is open-core:
- **Open-source**: the middleware, inference engine, local CLI, SQLite storage
- **SaaS**: multi-service schema graph, team dashboards, Slack/PagerDuty alerts,
  environment comparison (staging vs prod), compliance reporting

Every architecture decision in the open-source core should assume that a SaaS layer
might eventually sit above it — so the storage interface, snapshot format, and drift
report schema should be designed to be serializable and transmittable, not just local.
