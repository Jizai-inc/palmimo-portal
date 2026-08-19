"""``/api/v1/ssh-keys``: list, add, and delete registered public keys.

Always requires both a session and a provisioned device — unlike Wi-Fi,
there is no bootstrap reason to ever expose this on the open setup AP.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from palmimo_portal.api.deps import get_ssh_key_port, require_auth, require_full_session, require_provisioned
from palmimo_portal.api.errors import PortalError
from palmimo_portal.ports import (
    DuplicateKeyError,
    InvalidKeyFormatError,
    KeyNotFoundError,
    LastKeyError,
    SshKeyPort,
    SshKeysLockTimeoutError,
)


router = APIRouter(
    prefix="/api/v1/ssh-keys",
    tags=["ssh-keys"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)

LAST_KEY_CONFIRMATION = "last-key"


class SshKeyResponse(BaseModel):
    fingerprint: str
    key_type: str
    comment: str


class AddKeyRequest(BaseModel):
    public_key: str


class StatusResponse(BaseModel):
    status: str = "ok"


@router.get("")
def list_keys(ssh_keys: SshKeyPort = Depends(get_ssh_key_port)) -> list[SshKeyResponse]:
    """Return every registered key."""
    return [
        SshKeyResponse(fingerprint=key.fingerprint, key_type=key.key_type, comment=key.comment)
        for key in ssh_keys.list_keys()
    ]


@router.post("", status_code=201)
def add_key(body: AddKeyRequest, ssh_keys: SshKeyPort = Depends(get_ssh_key_port)) -> SshKeyResponse:
    """Validate, fingerprint, and register a public key.

    Raises:
        PortalError: 400 ``invalid_key_format`` if the text is not a
            well-formed public key; 409 ``duplicate_key`` if it is already
            registered; 409 ``ssh_keys_busy`` if the ``authorized_keys``
            lock could not be acquired in time (a concurrent add/delete
            holding it past :data:`~palmimo_portal.adapters.ssh_keys.SSH_KEYS_LOCK_TIMEOUT_SECONDS`).
    """
    try:
        key = ssh_keys.add_key(body.public_key)
    except InvalidKeyFormatError as error:
        raise PortalError(400, "invalid_key_format") from error
    except DuplicateKeyError as error:
        raise PortalError(409, "duplicate_key") from error
    except SshKeysLockTimeoutError as error:
        raise PortalError(409, "ssh_keys_busy") from error
    return SshKeyResponse(fingerprint=key.fingerprint, key_type=key.key_type, comment=key.comment)


@router.delete("/{fingerprint}")
def delete_key(
    fingerprint: str,
    confirm: str | None = Query(default=None),
    ssh_keys: SshKeyPort = Depends(get_ssh_key_port),
) -> StatusResponse:
    """Remove a registered key.

    Deleting the last remaining key requires ``?confirm=last-key`` —
    without it, the OS user would be left with no way back in over SSH.
    Enforced inside :meth:`~palmimo_portal.ports.SshKeyPort.delete_key`
    itself, not by a separate ``list_keys`` pre-check here, so two
    concurrent deletes cannot each observe two keys and both proceed,
    together emptying the file.

    Raises:
        PortalError: 404 ``key_not_found`` if no key has that fingerprint;
            409 ``last_key_deletion_requires_confirmation`` if it is the
            last key and ``confirm`` was not given; 409 ``ssh_keys_busy``
            if the ``authorized_keys`` lock could not be acquired in time
            (a concurrent add/delete holding it past
            :data:`~palmimo_portal.adapters.ssh_keys.SSH_KEYS_LOCK_TIMEOUT_SECONDS`).
    """
    try:
        ssh_keys.delete_key(fingerprint, allow_last=confirm == LAST_KEY_CONFIRMATION)
    except KeyNotFoundError as error:
        raise PortalError(404, "key_not_found") from error
    except LastKeyError as error:
        raise PortalError(409, "last_key_deletion_requires_confirmation") from error
    except SshKeysLockTimeoutError as error:
        raise PortalError(409, "ssh_keys_busy") from error
    return StatusResponse()
