# Releasing

How to cut a Palmimo Portal release: a SemVer tag and one GitHub Release.
CI attaches `palmimo-portal-static-<tag>.tar.gz` and a matching `.sha256`
sidecar to that release, which devices install through the Portal's own
Updater.

## 1. Versioning

- Tags are SemVer: `vX.Y.Z`, optionally with a pre-release suffix
  (`vX.Y.Z-rc1`).
- One tag = one GitHub Release.
- **Never delete or move a published tag** — a device may already be running
  it (the Updater checks out tags by name). Publish a newer tag instead of
  correcting an old one in place.
- A hyphenated tag (e.g. `v1.2.0-rc1`) is automatically created as a GitHub
  pre-release — see [What CI does](#4-what-ci-does) below. `GET
  repos/{repo}/releases/latest` — what a device's Updater checks — ignores
  pre-releases, so this keeps fleets off a build that is not meant for them
  yet, without relying on a maintainer remembering to tick a box.

## 2. Before tagging

1. Bump `version` in `pyproject.toml`.
2. Merge it, and confirm CI is green on `main`.

## 3. Tag and push

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

Tag the merged commit from step 2 above.

Who may do this: every member of the GitHub organization has write access
to this repository, and write access is all it takes to push a tag, run
the release workflow, and publish the resulting draft. There is no
separate release role on purpose. The workflow refuses a tag that is not
on `main`, and the human gate is step 5 -- reading the draft before
publishing it, since publishing is what devices react to.

## 4. What CI does

Pushing a `v*` tag triggers `.github/workflows/release.yml`:

1. Verifies the tagged commit is actually an ancestor of `main` — refuses to
   build a release from a tag pushed at a stray commit.
2. Builds the frontend on a pinned Ubuntu + Node runner (Node version taken
   from `frontend/.nvmrc`), then packages `palmimo_portal/static/` into
   `palmimo-portal-static-vX.Y.Z.tar.gz` and a matching `.sha256` checksum
   file. Building on the device was rejected: it would mean Node on the OS
   image and an `npm ci` on every update, for a non-deterministic result.
   Building in CI instead is deterministic because the runner is pinned,
   not because anything mechanically diffs the output against another
   build — the `Makefile`'s `check` target does not compare `static/` at
   all; a macOS-vs-Ubuntu byte-identical build is only ever checked
   historically, not on every run.
3. Creates the release as a **draft**, with GitHub's auto-generated notes
   (shaped by `.github/release.yml` — see [Labels](#6-labels-that-drive-the-notes)
   below), attaching the frontend asset and its `.sha256`. If the tag
   contains a hyphen, the release is created with `--prerelease` so it can
   never surface as `releases/latest` by a forgotten checkbox.

Re-running the workflow for a tag whose release is still a **draft** (e.g.
after fixing something) is safe — it re-uploads the assets rather than
failing. Re-running it for a tag whose release has already been
**published** fails on purpose: a device may already be running that
release, so its assets must never be silently replaced underneath it — cut
a new tag instead.

## 5. Publish

1. Open the draft release on GitHub.
2. Review the generated notes against the [template](#release-notes-template)
   below, and paste in the hand-written top block.
3. Tick **"Set as the latest release"** (skip this for a pre-release tag —
   see [Versioning](#1-versioning); the workflow already marked it a
   pre-release, so this checkbox should stay unticked).
4. Publish.

Devices see the new release the next time their Updater checks —
`releases/latest` only ever resolves to a published (non-draft,
non-prerelease) release.

## 6. Labels that drive the notes

Label a pull request with one of these **before merging** so
`.github/release.yml` files it under the right heading:

| Label | Heading |
|---|---|
| `breaking-change` | Breaking changes |
| `feature` | Features |
| `bug` | Fixes |
| `documentation` | Documentation |
| `skip-changelog` | excluded entirely |
| `dependencies` | excluded entirely |
| (none of the above) | Other changes |

## 7. Rollback

- **Fleet-wide**: publish a new tag with the fix — never delete or move a
  published one (see [Versioning](#1-versioning)).
- **Single device**: use the Portal dashboard's Update screen — "go back to
  the previous version" checks out the tag the device was running before its
  last update.

## 8. Verifying a release

```bash
gh release view vX.Y.Z
```

Confirms both assets (the tarball and its `.sha256`) are attached. To
verify the checksum locally, the same way a device's Updater does:

```bash
gh release download vX.Y.Z
sha256sum -c palmimo-portal-static-vX.Y.Z.tar.gz.sha256
```

**What the `.sha256` sidecar protects, and what it does not.** A device's
`GitUvUpdater` (and this same `sha256sum -c` check) verifies the downloaded
tarball's bytes match the sidecar exactly — it catches corruption or
truncation in transit, and a mismatch between the tarball and sidecar from a
half-finished or interrupted upload. It does **not** protect against a
compromised release origin: both files are fetched from, and generated by,
the same GitHub Release and the same CI job's `GITHUB_TOKEN` — an attacker
who can replace one can replace the other to match. Integrity-in-transit and
origin authenticity are different guarantees; this sidecar only gives the
first.

## 9. Testing a pre-release on a device

The default (`stable`) channel refuses to check for or apply a hyphenated
tag — see [Versioning](#1-versioning). To let one dev machine install an
`rc` published under [step 3](#3-tag-and-push), opt it into the
`prerelease` channel:

```bash
sudo systemctl edit palmimo-portal
```

Add, in the `[Service]` section of the override file the editor opens:

```ini
[Service]
Environment=PALMIMO_UPDATE_CHANNEL=prerelease
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart palmimo-portal
```

The dashboard's update check now resolves the newest published pre-release
(not `releases/latest`) and allows applying it. The UI never exposes this
setting — it is a deliberate opt-in for dev machines, not a fleet control.
To return the device to the stable channel, remove the override
(`sudo systemctl revert palmimo-portal`) and restart the service again.

## Release notes template

GitHub has no free-form release-template file — `.github/release.yml` only
shapes the auto-generated "What's Changed" section. Paste this hand-written
block above it when publishing:

```markdown
## Highlights

- 2-4 bullets on what this release is for

## Upgrade notes

- Anything a device owner must do or expect — e.g. "Portal restarts itself
  during the update; expect a few minutes of downtime."

## Known issues

- Anything shipped with a known gap, and its workaround if any

<!-- GitHub's generated "What's Changed" section, and the asset list, follow below -->
```
