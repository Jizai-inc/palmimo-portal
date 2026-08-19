"""Palmimo Portal — the device-hosted setup and dashboard web app.

Serves the Wi-Fi provisioning flow and, once set up, a dashboard for the
Portal password, SSH keys, and power operations. See
``doc/design/palmimo-portal.md`` and ``doc/design/palmimo-portal-technical.md``
in the monorepo for the product and technical design this implements (P1).

This package builds on no other first-party package for its P1 logic —
``palmimo_sdk``'s version is read only through package metadata (see
:mod:`palmimo_portal.api.system`), never imported.
"""
