# How Palmimo Portal Works

Palmimo Portal is the device's own setup/dashboard web UI — a FastAPI
backend (`palmimo_portal/`) plus a React frontend (`frontend/`), served
over the same origin and running as a systemd service on the device image.
A headless Raspberry Pi has no display to configure Wi-Fi on, so a fresh
device broadcasts its own hotspot (via `comitup`); Portal is what a phone
or laptop joining that hotspot talks to — first to join the robot to a
real network, then, once connected, as the dashboard for SSH key
management, reboot/shutdown, and self-updates.

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

## See also

- [Palmimo Portal source](../palmimo_portal/)
- [Frontend build/generate/test pipeline](../frontend/README.md)
- [Releasing](releasing.md)
