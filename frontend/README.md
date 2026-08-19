# Palmimo Portal frontend

The Palmimo Portal's setup/dashboard UI: React + TypeScript, built with Vite,
routed with TanStack Router (file-based, under `src/routes/`), and backed by
TanStack Query. `src/api/generated/` is generated from the backend's OpenAPI
schema by [orval](https://orval.dev) -- see `../Makefile` for the full
pipeline and the "API client autogeneration" section of
`doc/design/palmimo-portal-technical.md`.

## Commands

Run from `packages/palmimo_portal/` (the Makefile there wraps these):

- `make dev` -- Vite dev server (`npm run dev`), proxying `/api` to a backend
  already running on `:8080`.
- `make openapi` -- export the FastAPI app's OpenAPI schema to
  `frontend/openapi.json`.
- `make generate` -- regenerate `src/api/generated/` from `openapi.json` via
  orval (typed fetchers, TanStack Query hooks, and MSW mocks).
- `make build` -- type-check, then build the frontend into
  `../palmimo_portal/static/`, exactly where `app.py` serves it from.
- `make check` -- `openapi` + `generate` + `build`, then fail if either of
  the two committed artifact sets (`openapi.json`, `src/api/generated/`)
  came out different -- the drift gate CI runs.
- `make test` -- run the frontend's vitest unit tests (`npm test`), i.e.
  `src/**/*.test.{ts,tsx}`. See "Testing" below.
- `make fetch-static TAG=vX.Y.Z` -- download and verify a published
  release's build into `../palmimo_portal/static/`, for running the backend
  with a real UI without installing Node.

Equivalent npm scripts (`npm run dev`, `npm run build`, `npm run generate`,
`npm test`) work directly from this directory once `npm install` has run.

### `static/` is a build output, not committed

`../palmimo_portal/static/` (this build's output) is git-ignored, not
tracked: `.github/workflows/release.yml` builds it once per release tag and
attaches it to the GitHub Release as `palmimo-portal-static-<tag>.tar.gz` --
what a device's Updater downloads, verifies, and installs. See
doc/guides/releasing.md for the full release procedure.

## Testing

Unit tests ([Vitest](https://vitest.dev), configured separately from
`vite.config.ts` in `vitest.config.ts`) live alongside the code they cover
as `*.test.{ts,tsx}`. `src/test/setup.ts` fixes the active locale to English
and wires up [MSW](https://mswjs.io)'s Node server (`src/test/server.ts`);
`src/test/render.tsx`'s `renderWithProviders` wraps a component in a fresh
`QueryClient` per test. Prefer the generated
`get...MockHandler(overrideResponse)` helpers under `src/api/generated/*/`
with an explicit `overrideResponse` over the faker-based
`get...ResponseMock()` defaults, so a test's expectations stay deterministic.

## Stack notes

- **Styling**: Tailwind CSS v4 (`@tailwindcss/vite`, zero-config) plus a
  handful of [shadcn/ui](https://ui.shadcn.com) components vendored under
  `src/components/ui/` (source copied in, not an installed package -- see
  the MIT license file alongside them).
- **i18n**: `react-i18next`, with `src/i18n/en.json` / `ja.json` as the only
  place any user-visible string (including the backend's error codes --
  see `src/components/ApiErrorAlert.tsx`) lives. Default is the browser's
  language; `src/components/LanguageToggle.tsx` overrides it manually.
- **Routing guard**: `src/routes/__root.tsx`'s `beforeLoad` resolves
  `GET /api/v1/system/status` (plus a lightweight session probe, see
  `src/lib/authGate.ts`) into the screen the browser should be on, and
  redirects there -- the frontend half of the double gate; every endpoint it
  steers around also enforces its own access rule server-side.

## App shell

Two page shells hold the chrome common to every screen, both built on the
shared 56px `src/components/AppHeader.tsx`:

- `src/components/AuthShell.tsx` -- the provisioning screens (setup, login,
  change-password, wifi, wifi.waiting, status-error): the header, then a
  centered card (full-width on mobile, a bordered `max-w-[440px]` card on
  desktop).
- `src/components/AppShell.tsx` -- the authenticated dashboard family
  (`/dashboard`, `/ssh-keys`, `/power`): the header plus, on mobile, a fixed
  bottom tab bar, and on desktop, a collapsible left sidebar. A route using
  it is thin -- `<AppShell title={...}><SomePanel /></AppShell>`, with the
  route owning chrome/routing and a `*Panel` component owning the logic,
  unit-tested directly (see `SshKeysPanel.tsx`, `PowerPanel.tsx`,
  `WifiWaitingPanel.tsx`).

`src/lib/navigation.ts`'s `NAV_ITEMS` is the single source of truth for the
dashboard family's navigation: `AppShell`'s mobile tab bar and desktop
sidebar, and the dashboard's own quick-actions list, all render from it, and
`src/lib/authGate.ts`'s `DASHBOARD_FAMILY_PATHS` is derived from it so the
route guard can never drift from the nav. The i18n-parity contract
(`packages/palmimo_portal/tests/test_i18n_parity.py`) only recognizes
literal `t("...")` calls, so `src/lib/navLabels.ts` turns each `NavItem`'s
table-driven `labelKey`/`descriptionKey` back into an explicit `t("nav.…")`
call the static scan can see, rather than components calling
`t(item.labelKey)` directly.
