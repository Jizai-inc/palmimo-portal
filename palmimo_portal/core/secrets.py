"""Validation rules and log-masking shared by every :class:`~palmimo_portal.ports.SecretsStore`.

Kept here (not duplicated between the real adapter and the fake) so both
enforce the same name/value rules -- see design doc 3.2/4.2.
"""

from __future__ import annotations

import re

from palmimo_portal.ports import InvalidHostOwnerError, InvalidSecretNameError, InvalidSecretValueError, SecretsStore


#: Registered secret and git-credential store names. Also the shape a
#: manifest's `[env.<NAME>]` request name must match (core/manifest.py).
SECRET_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def validate_secret_name(name: str) -> None:
    """Raise :class:`InvalidSecretNameError` unless ``name`` matches :data:`SECRET_NAME_PATTERN`."""
    if not SECRET_NAME_PATTERN.fullmatch(name):
        raise InvalidSecretNameError(f"secret name must match {SECRET_NAME_PATTERN.pattern}, got {name!r}")


#: A normalized git-credential scope: a lowercase DNS-ish host, one ``/``, then an owner/org
#: name (design doc 4.2, e.g. ``github.com/Jizai-inc``). The owner half keeps its case --
#: GitHub org names are case-preserving, only the host is normalized.
HOST_OWNER_PATTERN = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+$")


def normalize_host_owner(host_owner: str) -> str:
    """Normalize a git-credential ``host_owner`` scope: trim whitespace/trailing ``/``, lowercase the host.

    Two callers naming the same credential differently (``GitHub.com/Foo/``
    vs. ``github.com/Foo``) must resolve to the same stored entry --
    :mod:`~palmimo_portal.adapters.secrets`/:mod:`~palmimo_portal.testing.fakes`
    run every ``host_owner`` argument through this before touching storage,
    and :func:`~palmimo_portal.core.apps.host_owner_from_url` runs its output
    through it too, so a lookup key built from a repo URL and one built from
    operator input always land on the same normalized string.

    Raises:
        InvalidHostOwnerError: the trimmed, lowercased-host string does not
            match :data:`HOST_OWNER_PATTERN` (e.g. no ``/owner`` part).
    """
    trimmed = host_owner.strip().rstrip("/")
    host, sep, owner = trimmed.partition("/")
    normalized = f"{host.lower()}{sep}{owner}"
    if not HOST_OWNER_PATTERN.fullmatch(normalized):
        raise InvalidHostOwnerError(f"host_owner must match {HOST_OWNER_PATTERN.pattern}, got {host_owner!r}")
    return normalized


def validate_secret_value(value: str) -> None:
    """Raise :class:`InvalidSecretValueError` if ``value`` contains a newline.

    A secret is later written as one ``KEY=value`` line in the app's
    ``env`` file (design doc 2.1) -- a newline in the value would let it
    inject a second, attacker-controlled env var line.
    """
    if "\n" in value or "\r" in value:
        raise InvalidSecretValueError("secret value must not contain a newline")


#: Replaces a git-credential env value in a log line or subprocess error tail.
GIT_CREDENTIAL_MASK = "***"

#: Env var names whose value must never reach a log line verbatim -- the
#: `GIT_CONFIG_VALUE_*` half of the `extraheader` mechanism (design doc 4.2)
#: is the credential token itself; `GIT_CONFIG_KEY_*`/`GIT_CONFIG_COUNT` name
#: no secret and are safe to log as-is.
_MASKED_ENV_PREFIXES = ("GIT_CONFIG_VALUE_",)


def mask_git_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with every git-credential value replaced by :data:`GIT_CREDENTIAL_MASK`."""
    return {key: (GIT_CREDENTIAL_MASK if key.startswith(_MASKED_ENV_PREFIXES) else value) for key, value in env.items()}


def mask_authorization_lines(text: str) -> str:
    """Drop any line containing an ``Authorization:`` header from subprocess stderr before logging it.

    A git credential helper failure can echo the extraheader value in its
    stderr (design doc 3.8).
    """
    return "\n".join(line for line in text.splitlines() if "authorization:" not in line.lower())


def secret_used_by(secrets: SecretsStore, name: str) -> list[str]:
    """Return the sorted, de-duplicated app names with any binding pointing at secret ``name``."""
    return sorted({app for app, _request_name in secrets.bindings_using(name)})


def collect_registered_secret_values(secrets: SecretsStore) -> list[str]:
    """Return every raw secret value and git credential token currently registered.

    For scrubbing an operator-facing text dump (``GET
    /apps/{name}/diagnostics``, design doc 3.2) before it leaves the
    process -- never for display. A value can reach that dump only via
    app-generated journal content, never something this codebase writes
    itself.
    """
    values = [secrets.get_secret_value(record.name) for record in secrets.list_secrets()]
    values += [secrets.get_git_credential(record.host_owner) for record in secrets.list_git_credentials()]
    return [value for value in values if value]


def mask_known_values(text: str, known_values: list[str]) -> str:
    """Replace every occurrence of any ``known_values`` entry in ``text`` with :data:`GIT_CREDENTIAL_MASK`."""
    masked = text
    for value in known_values:
        masked = masked.replace(value, GIT_CREDENTIAL_MASK)
    return masked
