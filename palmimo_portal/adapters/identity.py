"""Real :class:`~palmimo_portal.ports.IdentityStore`: the manufacturing-written identity file.

Schema, per palmimo-portal-technical.md's Authentication section::

    {"device_id": "<string>", "initial_password_hash": "<argon2id hash string>"}

Written once, at manufacturing time, to a path outside ``/var/lib/palmimo/``
-- by default the boot partition, so a factory reset (which only clears
``/var/lib/palmimo/``) does not erase it. The Portal only ever reads it.

Absence is a supported state, not an error: it means this SD card was
hand-flashed from the public image (the DIY path), and the device falls
back to the legacy open first-time-setup flow. A malformed (but present)
file is treated exactly the same as absent (logged at ERROR once) rather
than as a distinct locked state -- the identity file is not itself
security-bearing (see :class:`~palmimo_portal.ports.IdentityStore`'s
docstring), so failing open here does not weaken anything, and failing
closed would risk bricking a device over a corrupted boot-partition file.

A transient read failure (:class:`OSError`) is different and is never
conflated with either of those: ``/boot/firmware`` mounts separately from
the Portal's own filesystem, so if the Portal starts before that mount is
ready, "file absent" would otherwise get cached forever and a sticker/OEM
device would permanently become
:attr:`~palmimo_portal.core.identity.PortalAuthState.OPEN_SETUP` (claimable
by anyone) even after the real identity file appears. An :class:`OSError`
therefore reports :data:`~palmimo_portal.ports.IDENTITY_UNAVAILABLE`, is
never cached, and is logged at WARNING (rate-limited to once per instance --
see :attr:`FileIdentityStore._warned_unavailable`).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from palmimo_portal.ports import IDENTITY_UNAVAILABLE, Identity, IdentityStore, IdentityUnavailable


logger = logging.getLogger("palmimo_portal")


class FileIdentityStore(IdentityStore):
    """Reads the identity file at a fixed path, caching only a successful parse.

    A successful read is cached: the file never changes for the life of the
    device, so re-reading it on every request (a hot path --
    ``SessionMiddleware`` consults it while ``auth_state == "initial"``)
    would be pure waste.

    Clean absence and a transient read failure (:class:`OSError`) are both
    re-read on every call instead -- see the module docstring for why
    caching either would be a security bug, not just a missed optimization.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: Identity | None = None
        self._warned_unavailable = False
        self._warned_malformed = False

    def read_identity(self) -> Identity | IdentityUnavailable | None:
        if self._cached is not None:
            return self._cached
        identity = self._read_from_disk()
        if isinstance(identity, Identity):
            self._cached = identity
        return identity

    def read_identity_uncached(self) -> Identity | IdentityUnavailable | None:
        """Fresh disk read, bypassing :attr:`_cached` -- see :meth:`IdentityStore.read_identity_uncached`.

        Always reconciles :attr:`_cached` with what this fresh read found,
        since the identity file can change out from under a long-lived
        cache (an operator removing it, most notably):

        - A successful parse replaces :attr:`_cached` with the freshly read
          :class:`Identity`.
        - ``None`` (clean absence or a malformed file -- treated the same,
          see the module docstring) drops :attr:`_cached` back to ``None``:
          an operator who removed the file must see that reflected.
        - :data:`~palmimo_portal.ports.IDENTITY_UNAVAILABLE` (a transient
          :class:`OSError`) leaves :attr:`_cached` untouched -- must never
          overwrite a known-good cached identity with "gone"; see the
          module docstring's transient-read paragraph.
        """
        identity = self._read_from_disk()
        if isinstance(identity, Identity):
            self._cached = identity
        elif identity is None:
            self._cached = None
        return identity

    def _read_from_disk(self) -> Identity | IdentityUnavailable | None:
        """Read and parse the identity file, with no reference to :attr:`_cached` at all.

        Shared behind :meth:`read_identity` (cache first, fall back to this)
        and :meth:`read_identity_uncached` (always this), so the two public
        methods can never drift on what counts as absent, malformed, or
        unavailable.
        """
        try:
            exists = self._path.is_file()
        except OSError as error:
            return self._report_unavailable(error)
        if not exists:
            return None  # clean absence -- never cached, re-read on the next call

        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as error:
            return self._report_unavailable(error)

        try:
            data: Any = json.loads(text)
            if not isinstance(data, dict):
                raise TypeError(f"identity file top-level value is a {type(data).__name__}, expected an object")
            device_id, password_hash = data["device_id"], data["initial_password_hash"]
            if not isinstance(device_id, str) or not isinstance(password_hash, str):
                raise TypeError("identity file device_id/initial_password_hash must be strings")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if not self._warned_malformed:
                logger.error(
                    "identity file is malformed, treating as absent (DIY fallback): %s (%s)",
                    self._path,
                    error,
                )
                self._warned_malformed = True
            return None  # malformed -- also never cached, but the ERROR log is rate-limited

        return Identity(device_id=device_id, initial_password_hash=password_hash)

    def _report_unavailable(self, error: OSError) -> IdentityUnavailable:
        if not self._warned_unavailable:
            logger.warning(
                "identity file could not be read (transient error, not clean absence -- "
                "not caching, will retry on the next call): %s (%s)",
                self._path,
                error,
            )
            self._warned_unavailable = True
        return IDENTITY_UNAVAILABLE
