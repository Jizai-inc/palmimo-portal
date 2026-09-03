# Third-Party Notices

**What this list is.** The notices below itemize the dependencies this
repository *declares*, classified by the license each distribution declares
for itself. This is not an inventory of everything an installed environment
ends up containing: a wheel can bundle third-party binaries that its own
metadata never mentions, and what follows records the ones that have been
found rather than asserting there are no others. Nothing here substitutes for
the complete, machine-generated attribution each shipped artifact carries:

- **Frontend.** `make build` (via `frontend/scripts/generate-third-party-licenses.mjs`,
  run as `npm run licenses`) attributes every package in the union of (a) the
  production dependency closure walked from `package-lock.json` and (b) the
  package names `frontend/vite.config.ts`'s `record-bundled-packages` plugin
  observed actually contributing a module to the real build (this is how a
  devDependency whose *output* still ships, like `tailwindcss`'s preflight
  CSS reset, gets attributed too). Each entry is marked `In bundle: yes/no`
  in the rendered notice. License text is taken from an installed
  `LICENSE`/`COPYING` file when there is one; failing that, from
  `package.json`'s `author` plus a substantive README `# License` section;
  failing that, from a checked-in fallback entry in
  `frontend/scripts/third-party-license-overrides.json` (used only when
  upstream genuinely ships no LICENSE file for that exact version — see that
  file's own header for the schema and citation convention). A package with
  no usable text anywhere in that chain fails the build rather than shipping
  unattributed. The result lands at
  `palmimo_portal/static/THIRD_PARTY_LICENSES.txt`, which ships inside the
  release tarball `.github/workflows/release.yml` attaches to each
  GitHub Release (verified present in that tarball by the same workflow step
  that packages it) and, incidentally, becomes servable by the Portal itself
  at `/THIRD_PARTY_LICENSES.txt`.
- **Python.** The SD image build collects each installed package's license
  file from its venv's `*.dist-info/` directory; that step lives in the
  [palmimo-image](https://github.com/Jizai-inc/palmimo-image) repository, not
  here, since it depends on the image's fully resolved Python environment.

None of the components below are vendored or distributed with `palmimo-portal`
itself — they are installed as regular PyPI dependencies.

## Base dependency

Applies to every install of `palmimo-portal`.

### fastapi

- License: MIT
- Source: https://github.com/fastapi/fastapi

### uvicorn

- License: BSD-3-Clause
- Source: https://github.com/encode/uvicorn
- Installed with the `standard` extra (`httptools`, `uvloop`, `watchfiles`,
  and friends), each of which is itself MIT or BSD-3-Clause licensed.

### argon2-cffi

- License: MIT
- Source: https://github.com/hynek/argon2-cffi
- Password hashing (argon2id) for the Portal's local login. Pulls in
  `argon2-cffi-bindings` (MIT/CC0 dual, links the Apache-2.0 licensed
  reference `libargon2`).

### itsdangerous

- License: BSD-3-Clause
- Source: https://github.com/pallets/itsdangerous
- Signs the Portal's session cookie.

### dbus-fast

- License: MIT
- Source: https://github.com/Bluetooth-Devices/dbus-fast
- Async D-Bus client used by the real `NetworkPort` (comitup) and
  `SystemPort` (systemd-logind) adapters to talk to the system bus.

## Frontend (`frontend/`, bundled into `static/`)

Built with npm/Vite; the compiled bundle (not the npm packages themselves) is
what ships in `static/`. All MIT-licensed, permissive and compatible with this
project's Apache-2.0 license.

### shadcn/ui

- License: MIT
- Source: https://ui.shadcn.com
- Distributed as source you copy into your own project rather than an
  installed package, so the components under `frontend/src/components/ui/`
  are vendored by hand (adapted from shadcn/ui's standard output) rather than
  fetched by its CLI. The MIT license text travels with them at
  `frontend/src/components/ui/LICENSE`.

### Radix UI

- License: MIT
- Source: https://www.radix-ui.com
- The unstyled primitives shadcn/ui's vendored components (`button`, `label`,
  `alert-dialog`) build on (`@radix-ui/react-slot`, `@radix-ui/react-label`,
  `@radix-ui/react-alert-dialog`).

### React, TanStack Router, TanStack Query, react-i18next, i18next, class-variance-authority, clsx, tailwind-merge, lucide-react

- License: MIT
- Installed as regular npm dependencies (see `frontend/package.json`) and
  bundled into `static/assets/` by `vite build`.
