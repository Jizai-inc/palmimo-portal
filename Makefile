# Palmimo Portal build pipeline: FastAPI (Pydantic models) -> openapi.json ->
# orval -> TanStack Query hooks + MSW mocks -> vite build -> static/.
#
# Run from the repository root. `static/` is no longer a committed
# artifact: CI builds it once, when a release tag is pushed
# (.github/workflows/release.yml), and attaches it to the GitHub Release as
# a tarball; the device's Updater downloads it instead of building on the
# device (see doc/releasing.md). `check` is the drift gate CI still
# runs on every PR: it regenerates the two artifacts that *are* committed
# (openapi.json, src/api/generated/) and fails if regenerating produced a
# diff, so a schema change that never made it into the frontend client (or a
# hand-edit of a generated file) is caught mechanically -- then `build`
# proves the frontend still builds at all, without diffing its output.
# `test` runs the frontend's vitest unit tests (see frontend/vitest.config.ts)
# -- separate from `check`, and not one of its prerequisites, since it
# exercises hand-written component/lib logic rather than the
# generated-artifact pipeline.

UV := uv run
NPM := npm --prefix frontend

# Mirrors palmimo_portal/core/update.py's STATIC_ASSET_NAME_TEMPLATE -- the
# single source of truth for the release asset's name. Kept in sync by
# tests/test_static_asset_name.py, which checks this literal pattern (with
# `$(TAG)` swapped for Python's `{tag}`) against the template. (The
# `.github/workflows/release.yml` side of this, which does its own `${TAG}`
# check against the same template, lives in this repository too -- see
# tests/contracts/test_release_workflow_contract.py.)
# `fetch-static` no longer interpolates this itself (the CLI it calls
# resolves the name from the same Python-side template directly); this
# definition stays as the one place the pattern is spelled out in Make
# syntax, echoed by the recipe below so a run still names the asset it
# is fetching.
STATIC_ASSET_NAME := palmimo-portal-static-$(TAG).tar.gz
# The GitHub repository `fetch-static` downloads a release asset from --
# same default as palmimo_portal/settings.py's DEFAULT_UPDATE_REPO,
# overridable for a fork.
UPDATE_REPO ?= Jizai-inc/palmimo-portal

.PHONY: openapi generate build dev check test fetch-static

# Export the FastAPI app's OpenAPI schema (built on fake adapters -- see
# palmimo_portal/openapi_export.py) to frontend/openapi.json. Runs through the
# workspace venv's Python, never a bare `python`.
openapi:
	$(UV) python -m palmimo_portal.openapi_export frontend/openapi.json

# Generate typed fetchers, TanStack Query hooks, and MSW mocks from
# openapi.json into frontend/src/api/generated/.
generate: openapi
	$(NPM) run generate

# Build the frontend and land the output at palmimo_portal/static/, exactly
# where app.py serves it from (see _mount_frontend in app.py). `licenses`
# runs after `build` (not before): it reads node_modules/<name>/ directly,
# and needs nothing from the build output itself, but landing it under
# static/ only makes sense once static/ exists -- and gives the Portal a
# second, incidental way to serve it, at /THIRD_PARTY_LICENSES.txt.
build:
	$(NPM) run build
	$(NPM) run licenses

# Vite dev server, proxying /api to a backend already running on :8080
# (`uv run --project .. python -m palmimo_portal`, PALMIMO_ADAPTERS=fake).
dev:
	$(NPM) run dev

# The frontend's vitest unit tests (src/**/*.test.{ts,tsx}) -- see
# frontend/vitest.config.ts and frontend/src/test/ for the test harness.
test:
	$(NPM) test

# The drift gate: regenerate the two committed artifact sets, then fail if
# doing so changed either. `build` also runs (proving the frontend still
# builds end to end) but its output, static/, is not part of the diff --
# see the header comment above for why. Run this locally before pushing,
# and in CI.
#
# `git diff --exit-code` alone only catches a MODIFIED tracked file -- a
# schema change that adds a brand-new generated file (a fresh orval hook
# module) shows up as untracked, not modified, and `git diff` says nothing
# about it at all. `git status --porcelain` catches both: any line for
# these two paths, tracked or not, means the working tree no longer matches
# what was committed.
#
# On failure, name the stale files and the exact fix so the CI log's last
# lines answer "what do I do" without archaeology.
check: generate build
	@stale="$$(git status --porcelain -- frontend/openapi.json frontend/src/api/generated/)"; \
	if [ -n "$$stale" ]; then \
		echo ""; \
		echo "Committed build artifacts are stale. Regenerating changed:"; \
		echo "$$stale"; \
		echo ""; \
		echo "Fix: run 'make check' at the repository root and commit the diff."; \
		git --no-pager diff --stat -- frontend/openapi.json frontend/src/api/generated/; \
		exit 1; \
	fi

# Download and verify a release's frontend build into palmimo_portal/static/
# -- for a developer or tester who wants the UI without installing Node.
# Calls the same download/verify/extract/swap functions GitUvUpdater's
# `assets`/`install-assets` steps run on a device (see
# palmimo_portal/adapters/static_asset.py and palmimo_portal/fetch_static.py)
# instead of a separate shell reimplementation of that sequence, so there is
# one place, not two, that knows how to fetch a release's frontend asset
# safely. Usage: `make fetch-static TAG=v1.2.3`.
fetch-static:
	@if [ -z "$(TAG)" ]; then \
		echo "Usage: make fetch-static TAG=vX.Y.Z"; \
		exit 1; \
	fi
	@echo "Fetching $(STATIC_ASSET_NAME) from $(UPDATE_REPO)"
	$(UV) python -m palmimo_portal.fetch_static --tag $(TAG) --repo $(UPDATE_REPO) --dest palmimo_portal/static
