# apinfer

> Infer your real API schema from live traffic — not from docs you forgot to update.

**apinfer** is a passive schema observation tool for Python backends. It sits quietly
inside your application, watches real HTTP traffic, and builds a ground-truth picture
of what your API actually does — field by field, endpoint by endpoint. When that
picture changes, it tells you exactly what broke and how serious it is.

No OpenAPI spec required. No configuration files. No external services.

---

## The problem

Your API and its documentation are two separate things that drift apart the moment
you stop paying attention.

A field silently changes from `integer` to `string` after a database migration.
A required field starts disappearing in 8% of responses after a config change.
A new field appears in production that nobody documented. Your OpenAPI spec still
describes the world as it was six months ago.

None of this gets caught by your tests. Tests check business logic — not that
`user_id` is still the type every consumer expects it to be.

The standard approach is to treat the spec as the source of truth and validate
the API against it. But that only works if the spec is accurate. Most of the time,
it isn't. You end up validating a lie against a lie and calling it green.

**apinfer inverts this.** The running application is the source of truth. The spec,
if you have one, is something to compare against — not something to trust blindly.

---

## How it works

Add one line to your app. apinfer installs as middleware and passively intercepts
every request and response as it flows through. It normalizes the payloads, infers
a JSON schema from the real data it observes, and saves a versioned snapshot to a
local file. No traffic leaves your machine.

When you run `apinfer diff`, it compares the latest snapshot against the previous
one and shows you exactly what changed — which fields appeared, disappeared, changed
type, or shifted from required to optional. Each change is classified by severity so
you know immediately whether it's a breaking change or not.

```
apinfer report

  GET /users/{id}  ·  200  ·  observed 1,240 responses

  user_id     integer   required    100%
  name        string    required    100%
  avatar      string    optional     67%   ← present in 67% of responses
  metadata    object    optional     12%   ← new field, not in your spec


apinfer diff

  GET /orders/{id}  ·  200

  BREAKING     price      string → number
  BREAKING     tax_rate   field disappeared (was required)
  non-breaking referral   new optional field
```

If you have an OpenAPI spec, apinfer can compare the inferred schema against it
and surface every discrepancy between what your spec promises and what your API
delivers. If you don't have a spec at all, apinfer still works — it becomes the
spec, tracking how your contract evolves over time.

---

## Features

**Spec-free by default.** Works on any Python backend regardless of whether you
have OpenAPI documentation. apinfer learns your contract from scratch by observing
real traffic.

**Passive and non-invasive.** Middleware runs on the request path only long enough
to capture the exchange. All inference and analysis happens asynchronously, off the
critical path. No measurable latency added to your responses.

**Breaking change detection.** Every detected change is classified as breaking,
non-breaking, or informational. A field changing type is breaking. A new optional
field appearing is not. You see the difference immediately.

**Presence rates.** apinfer tracks not just whether a field exists in your schema,
but how consistently it appears. A field present in 60% of responses is fundamentally
different from one present in 100%, and your consumers deserve to know that.

**Versioned snapshots.** Every schema observation is saved as a timestamped snapshot.
You can diff any two points in time, not just the latest against the previous.

**OpenAPI comparison (optional).** Point apinfer at your spec file and it will
tell you every place your running API diverges from what the spec describes. Works
as a CI gate — fail the build if the implementation and the spec have drifted apart.

**pytest integration.** A first-class pytest plugin lets you assert that your API
contract hasn't changed unexpectedly as part of your test suite. Catch breaking
changes in CI before they reach production.

**Zero external dependencies for the core experience.** Everything runs locally.
Snapshots are stored in a single SQLite file in your project directory. No database
server, no cloud account, no Docker.

**Framework support.** Works with FastAPI, Django, Flask, Starlette, Litestar,
and Falcon out of the box via ASGI and WSGI middleware variants.

---

## What apinfer is not

It is not a linter for your OpenAPI file. It is not a mock server. It is not a
load testing tool or a security scanner. It does not modify your responses or
proxy your traffic anywhere. It watches, infers, and reports — nothing more.

---

## Status

apinfer is under active development and not yet released. The core inference
engine, storage layer, and ASGI middleware are being built first. A working MVP
is the immediate target — one middleware line, a handful of CLI commands, no
configuration required.

If this solves a problem you've run into, watch the repository or open an issue
describing your use case. Early feedback shapes what gets built first.

---

## Contributing

The project is not yet accepting code contributions while the foundational
architecture is being established. Once the core is stable, contribution
guidelines will be published. In the meantime, opening issues with real-world
scenarios where API schema drift has caused you pain is genuinely useful.

---

## License

MIT
