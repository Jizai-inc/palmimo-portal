# How Palmimo Portal Works

Palmimo Portal is the device's own setup/dashboard web UI — a FastAPI
backend (`palmimo_portal/`) plus a React frontend (`frontend/`), served
over the same origin and running as a systemd service on the device image.
A fresh device offers Wi-Fi QR setup through its camera and face display,
and a hotspot (via `comitup`) for a phone or laptop to open Portal. Once
connected, Portal provides SSH key management, reboot/shutdown, apps, and
self-updates.

## Auth model

Portal has exactly one account and no server-side session table. A password
is hashed with argon2id and, on success, exchanged for a signed, timestamped
token (`itsdangerous`) carried in an `HttpOnly`/`SameSite=Strict` cookie —
verification is purely a signature/expiry check against the stored signing
key, not a database lookup. Logging out only deletes the cookie; a token
copied off the browser before that stays valid until it expires or the
signing key rotates, which happens on every password change. Repeated failed
logins lock out for a fixed window. A manufactured, identity-carrying device
(one with a printed sticker password) additionally exposes an
unauthenticated, throttled credential reset for a locked-out owner; a DIY
device has no such reset, since without a sticker password it would just
reopen anonymous setup.

## Update model

"Update" means this device's own clone of this repository, one GitHub
Release at a time — no channel selection and no arbitrary tags. Checking,
applying, and rolling back all go through the same durable, logged job state,
so a failed apply always leaves a "go back to the previous tag" path.
Applying fetches the release tag, checks it out, resyncs dependencies, and
restarts the Portal's own systemd unit; the frontend build itself is not
committed to the checkout — it ships as a separate GitHub Release asset that
the updater downloads and swaps into place (see [Releasing](releasing.md)).

## Face display

Portal requires platform bundle version 11 or newer. Update the platform
from Settings → Software update if app installation or startup reports
`platform_not_ready`.

### Starting an app

`POST /api/v1/apps/{id}/start` accepts a background start job with HTTP 202
and a `job` object. Acceptance does not mean the app is running: follow
`GET /api/v1/apps/jobs/{job_id}` until `done` or `failed`. The UI shows firmware,
asset-transfer, and launch progress; closing the dialog does not cancel the job.
Synchronous precheck failures still reject the request before creating a job.

For apps declaring `motor_display`, Portal selects the protocol requested by
the installed SDK before launch. Switching protocols takes about three minutes;
writing cannot be stopped once it begins. The UI asks for confirmation when
the app's requirement differs from the selected protocol, or warns that a
rewrite may be needed when the selected protocol is unknown. Apps requiring
v1 carry a legacy-firmware alert. A missing or undeclared display allows launch
with a warning; a busy display defers recovery or fails a requested start so
it can be retried.

### App assets and pending synchronization

Portal accepts manifest schemas 1 and 2. Schema 2 adds `[assets.<name>]` for
app-owned faces and optional parameter-slot descriptions. Declaring assets
requires an SDK that requests protocol 2 and the `motor_display` device.
During install or update, Portal converts the declared source files and checks
slots and flash capacity before registering them; invalid assets fail the job
before the installed app is replaced. Starting a protocol-2 display app
synchronizes its assets before launch.

A busy synchronization leaves a pending marker. The shared banner shows
pending or running synchronization and offers **Sync now**. Portal retries
pending assets periodically, after an app stops, and on a later app start.
Only a `synced` result clears pending; `skipped` leaves it for a later attempt.
Local retries do not require an NTP-synchronized clock.

### Replacing standard assets

Settings → Face display lists standard assets and their overrides. Select a
source file to preview conversion warnings, parameter slots that will no longer
work, and apps that would no longer fit. Saving applies the override and tries
to synchronize it; a busy display leaves synchronization pending. Restore an
entry to its standard version to remove an override. Overrides whose standard
name has disappeared can only be deleted.

### Wi-Fi QR transport and specifications

The QR reader uses `STREAM` when the face reports protocol 2, and retains
`FRAME` compatibility for protocol 1 or an unanswered protocol probe. Transport
and retry details are in [Wi-Fi QR reader](wifi-qr-reader.md#face-display-transport).

The format and wire specifications are maintained upstream:

- [display-protocol.md](https://github.com/Jizai-inc/palmimo-devkit/blob/main/doc/reference/display-protocol.md)
- [app-manifest.md](https://github.com/Jizai-inc/palmimo-devkit/blob/main/doc/reference/app-manifest.md)
- [display-assets.md](https://github.com/Jizai-inc/palmimo-image/blob/main/doc/display-assets.md)

## App repair

Retired official apps offer repair only when the manifest or virtual environment is missing, or when launcher exit code 78 marks the app as needing repair.

## See also

- [Palmimo Portal source](../palmimo_portal/)
- [Frontend build/generate/test pipeline](../frontend/README.md)
- [Releasing](releasing.md)
