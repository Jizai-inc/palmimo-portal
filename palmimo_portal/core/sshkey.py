"""Pure ``authorized_keys`` line parsing, validation, and fingerprinting.

Lives here rather than in :mod:`palmimo_portal.adapters.ssh_keys` (which
still re-exports these names) because it touches no filesystem, D-Bus, or
other OS state -- exactly the kind of logic ``core/`` is for, and usable by
:mod:`palmimo_portal.testing.fakes` without importing from ``adapters/``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct

from palmimo_portal.ports import InvalidKeyFormatError


_KEY_TYPES = (
    "ssh-rsa",
    "ssh-ed25519",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)


def _wire_format_type(decoded: bytes) -> str:
    """Return the key-type string encoded in an SSH wire-format public-key blob.

    Wire format (RFC 4253 §6.6): 4-byte big-endian length prefix, then an
    ASCII type string, which must match the plaintext type field of the
    ``authorized_keys`` line -- otherwise base64 garbage or a truncated
    key could register as an unusable key.

    Raises:
        InvalidKeyFormatError: too short for a length prefix, the declared
            length overruns the buffer, or the type bytes are not ASCII.
    """
    if len(decoded) < 4:
        raise InvalidKeyFormatError("wire format blob too short for a length prefix")
    (length,) = struct.unpack(">I", decoded[:4])
    if 4 + length > len(decoded):
        raise InvalidKeyFormatError("wire format length prefix overruns the blob")
    try:
        return decoded[4 : 4 + length].decode("ascii")
    except UnicodeDecodeError as error:
        raise InvalidKeyFormatError("wire format type field is not ASCII") from error


def parse_authorized_key(line: str) -> tuple[str, str]:
    """Validate one ``authorized_keys`` line and return ``(key_type, comment)``.

    Accepts the bare ``type base64-blob [comment]`` form. Options fields
    (``command="...",...``) are not supported.

    A ``line`` with an embedded newline or carriage return is rejected
    before any further parsing: ``str.split(None, ...)`` treats those as
    ordinary whitespace, so a malicious multi-line ``public_key`` would
    otherwise smuggle an attacker-chosen second line into
    ``authorized_keys`` via the parsed "comment".

    The decoded blob must also be a well-formed SSH wire-format key whose
    embedded type matches the declared ``key_type`` (see
    :func:`_wire_format_type`), not just valid base64.

    Raises:
        InvalidKeyFormatError: the line contains a newline or carriage
            return, does not parse as ``type blob``, the type is not
            recognized, the blob is not valid base64, or the decoded blob
            is not a well-formed wire-format key of the declared type.
    """
    if "\n" in line or "\r" in line:
        raise InvalidKeyFormatError(line)
    parts = line.strip().split(None, 2)
    if len(parts) < 2:
        raise InvalidKeyFormatError(line)
    key_type, blob = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""
    if key_type not in _KEY_TYPES:
        raise InvalidKeyFormatError(line)
    try:
        decoded = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as error:
        raise InvalidKeyFormatError(line) from error
    if not decoded:
        raise InvalidKeyFormatError(line)
    if _wire_format_type(decoded) != key_type:
        raise InvalidKeyFormatError(line)
    return key_type, comment


def fingerprint_key(line: str) -> str:
    """Return the ``SHA256:...`` fingerprint of an ``authorized_keys`` line.

    The digest is base64**url**-encoded (``-``/``_`` instead of ``+``/``/``),
    padding stripped, rather than ``ssh-keygen -lf``'s standard-base64
    format: this fingerprint travels in a URL path
    (``DELETE /api/v1/ssh-keys/{fingerprint}``), and a standard-base64
    ``/`` would split the path into extra segments the route cannot match.
    It is an opaque identifier this API mints and reads back itself, not a
    value compared against an external tool's output.
    """
    _, blob = line.strip().split(None, 2)[:2]
    decoded = base64.b64decode(blob, validate=True)
    digest = hashlib.sha256(decoded).digest()
    return f"SHA256:{base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')}"
