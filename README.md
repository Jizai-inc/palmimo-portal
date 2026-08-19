# Palmimo Portal

Palmimo Portal is the device's own always-on setup/dashboard web UI: a
FastAPI backend plus a React frontend, reachable at `http://<hostname>.local/`
(or the device's own Wi-Fi hotspot before it has joined a network). It walks
a fresh device through Wi-Fi setup, then serves an authenticated dashboard
for SSH key management, power controls, and self-updates.

See [How Palmimo Portal works](doc/palmimo-portal.md) for the auth and
update model.

## Status

Palmimo Portal ships preinstalled on the Palmimo device image as a systemd
service — nothing to install separately on a built robot. This repository is
the update source: a device's own Updater pulls new versions from here, one
tagged GitHub Release at a time (see [Releasing](doc/releasing.md)).

## Quick dev setup

```bash
uv sync --dev
uv run python -m palmimo_portal
```

`PALMIMO_ADAPTERS=fake` is the default, so the backend above serves on
`:8080` against in-memory fakes — no D-Bus, systemd, or real filesystem
state required. Set `PALMIMO_ADAPTERS=real` to run against the OS-backed
adapters instead (comitup and logind over D-Bus, the filesystem for the
rest) — only meaningful on a Linux host with those services present.

```bash
make dev          # Vite dev server, proxying /api to the backend above
make check         # drift gate: regenerates openapi.json + the frontend
                    # API client, fails on a diff, and builds the frontend
make test           # frontend unit tests (vitest)
uv run pytest        # backend unit + integration tests
```

## Adapters: fake vs. real

Every port (network, system, SSH keys, state, identity, releases, updater)
has two implementations. `"fake"` wires in-memory fakes for every port —
what the whole test suite and local development run against. `"real"` wires
the OS-backed adapters — comitup and logind over D-Bus for network and
system control, the filesystem for the rest — and is what the device image
actually runs. See [`palmimo_portal/wiring.py`](palmimo_portal/wiring.py).

## How updates reach devices

A tagged GitHub Release here (`vX.Y.Z`) carries a `palmimo-portal-static-<tag>.tar.gz`
frontend build as an asset, built by CI and attached automatically. A
device's Updater fetches the tag, checks it out, resyncs dependencies with
`uv sync --frozen`, downloads and verifies that frontend asset, and restarts
the Portal's own systemd unit. See [doc/releasing.md](doc/releasing.md) for
the full release procedure.

## Repository layout

```
palmimo_portal/        FastAPI backend
  adapters/               OS-backed port implementations (comitup, systemd, filesystem, ...)
  api/                    FastAPI routers, middleware, and app assembly
  core/                   OS-independent, HTTP-independent use-case logic
  testing/fakes.py        In-memory fakes for every port
  ports.py                The port protocols every adapter/fake implements
  wiring.py               Builds the concrete adapter set create_app wires in
frontend/               React + TanStack Router/Query dashboard
  src/api/generated/       Typed client generated from openapi.json (orval)
tests/                  Backend unit + integration tests
tests/contracts/        Repository-wide contracts (import discipline, language, hygiene, release workflow)
doc/                    Design docs and the release guide
.github/workflows/      CI and the release pipeline
```

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party attributions
are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[NOTICE](NOTICE).
