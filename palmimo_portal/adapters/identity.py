"""Real :class:`~palmimo_portal.ports.IdentityStore`: the manufacturing-written identity file.

Schema::

    {"device_id": "<string>", "initial_password_hash": "<argon2id hash string>"}

Written once, at manufacturing time, to a path outside ``/var/lib/palmimo/``
(by default the boot partition, so a factory reset does not erase it); the
Portal only ever reads it. Absence, malformed content, and a transient
:class:`OSError` are three distinct outcomes with different caching/security
semantics -- see :class:`~palmimo_portal.ports.IdentityStore` for the
contract this module implements.
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

    A successful read is cached: the file never changes for the device's
    life, and re-reading on every request (``SessionMiddleware`` consults
    it while ``auth_state == "initial"``) would be pure waste. Clean
    absence and a transient :class:`OSError` are re-read every call instead
    -- see :class:`~palmimo_portal.ports.IdentityStore` for why caching
    either would be a security bug, not just a missed optimization.
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

        Reconciles :attr:`_cached` with the fresh result: a successful parse
        replaces it, clean absence/malformed drops it to ``None`` (an
        operator removing the file must see that reflected), and a
        transient :data:`~palmimo_portal.ports.IDENTITY_UNAVAILABLE` leaves
        it untouched -- must never overwrite a known-good cached identity
        with "gone".
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
