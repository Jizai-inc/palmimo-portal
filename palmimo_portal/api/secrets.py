"""``/api/v1/secrets`` and ``/api/v1/git-credentials``: write-only value store, never read back.

No endpoint here ever returns a secret value or git credential token --
``GET /secrets`` answers with names and update times only (design doc 3.2).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_secrets_store,
    get_state_store,
    require_auth,
    require_full_session,
    require_provisioned,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.apps import clear_credential_rejected
from palmimo_portal.core.secrets import normalize_host_owner, secret_used_by
from palmimo_portal.ports import (
    AppsStateFileState,
    InvalidHostOwnerError,
    InvalidSecretNameError,
    InvalidSecretValueError,
    SecretInUseError,
    SecretNotFoundError,
    SecretsStore,
    StateStore,
)


logger = logging.getLogger("palmimo_portal")

router = APIRouter(
    tags=["secrets"],
    dependencies=[Depends(require_provisioned), Depends(require_auth), Depends(require_full_session)],
)


class SecretRecordInfo(BaseModel):
    name: str
    updated_at: float
    used_by: list[str]


class SecretsListResponse(BaseModel):
    secrets: list[SecretRecordInfo]


class SecretValueRequest(BaseModel):
    value: str


class GitCredentialRecordInfo(BaseModel):
    host_owner: str
    updated_at: float
    rejected_at: float | None = None


class GitCredentialsListResponse(BaseModel):
    credentials: list[GitCredentialRecordInfo]


@router.get("/api/v1/secrets")
def list_secrets(store: SecretsStore = Depends(get_secrets_store)) -> SecretsListResponse:
    """List every registered secret's name and last-update time. Never returns a value."""
    return SecretsListResponse(
        secrets=[
            SecretRecordInfo(name=r.name, updated_at=r.updated_at, used_by=secret_used_by(store, r.name))
            for r in store.list_secrets()
        ]
    )


@router.put("/api/v1/secrets/{name}")
def put_secret(
    name: str, body: SecretValueRequest, store: SecretsStore = Depends(get_secrets_store)
) -> SecretRecordInfo:
    """Register or overwrite a secret value.

    Raises:
        PortalError: 422 ``invalid_secret_name`` if ``name`` does not match
            ``^[A-Z][A-Z0-9_]{0,63}$``; 422 ``invalid_secret_value`` if the
            value contains a newline.
    """
    try:
        store.set_secret(name, body.value)
    except InvalidSecretNameError as error:
        raise PortalError(422, "invalid_secret_name") from error
    except InvalidSecretValueError as error:
        raise PortalError(422, "invalid_secret_value") from error
    updated = next(r for r in store.list_secrets() if r.name == name)
    logger.info("secrets: set name=%s", name)
    return SecretRecordInfo(name=updated.name, updated_at=updated.updated_at, used_by=secret_used_by(store, name))


@router.delete("/api/v1/secrets/{name}")
def delete_secret(
    name: str, store: SecretsStore = Depends(get_secrets_store), state_store: StateStore = Depends(get_state_store)
) -> None:
    """Delete a registered secret.

    Raises:
        PortalError: 404 ``secret_not_found``; 409 ``secret_in_use`` (with
            the apps/request-names still bound to it) if a binding
            references it.
    """
    try:
        store.delete_secret(name)
    except SecretNotFoundError as error:
        raise PortalError(404, "secret_not_found") from error
    except SecretInUseError as error:
        apps = state_store.read_apps_state().apps
        raise PortalError(
            409,
            "secret_in_use",
            # `app_id` is bindings.json's key (design doc 3.9); `app_name` is the ledger's
            # display name for it, falling back to the id itself for a binding whose app
            # record is missing (not a shape a real ledger has, but a caller must not crash).
            users=[
                {"app_id": app_id, "app_name": apps[app_id].name if app_id in apps else app_id, "request": req}
                for app_id, req in error.users
            ],
        ) from error
    logger.info("secrets: deleted name=%s", name)


@router.get("/api/v1/git-credentials")
def list_git_credentials(store: SecretsStore = Depends(get_secrets_store)) -> GitCredentialsListResponse:
    """List every registered ``host/owner`` credential's metadata. Never returns a token."""
    return GitCredentialsListResponse(
        credentials=[
            GitCredentialRecordInfo(host_owner=r.host_owner, updated_at=r.updated_at, rejected_at=r.rejected_at)
            for r in store.list_git_credentials()
        ]
    )


@router.put("/api/v1/git-credentials/{host_owner:path}")
def put_git_credential(
    host_owner: str,
    body: SecretValueRequest,
    store: SecretsStore = Depends(get_secrets_store),
    state_store: StateStore = Depends(get_state_store),
) -> GitCredentialRecordInfo:
    """Register or overwrite a GitHub ``host/owner`` credential token (design doc 4.2). Never returned by any endpoint.

    ``host_owner`` is normalized (lowercase host, trimmed) before storage --
    see :func:`~palmimo_portal.core.secrets.normalize_host_owner` -- so
    ``GitHub.com/Foo/`` and ``github.com/Foo`` land on the same entry.

    Also clears :attr:`~palmimo_portal.ports.AppRecord.credential_rejected`
    on every app scoped to ``host_owner`` -- a rewritten credential must
    resume periodic update checks (design doc 3.6), not stay skipped until
    one happens to succeed. Skipped when ``apps.json`` is corrupt: writing
    ``clear_credential_rejected``'s result back would replace the corrupt
    file with a fresh empty ledger, destroying whatever ``POST
    /apps/reset`` was meant to recover instead of the credential save.

    Raises:
        PortalError: 422 ``invalid_host_owner`` if ``host_owner`` does not
            normalize to ``host/owner`` shape; 422 ``invalid_secret_value``
            if the token contains a newline.
    """
    try:
        store.set_git_credential(host_owner, body.value)
    except InvalidHostOwnerError as error:
        raise PortalError(422, "invalid_host_owner") from error
    except InvalidSecretValueError as error:
        raise PortalError(422, "invalid_secret_value") from error
    host_owner = normalize_host_owner(host_owner)
    if state_store.apps_state_file_state() is not AppsStateFileState.PRESENT:
        logger.warning("git-credential: set host=%s, apps.json ledger is unavailable, skipped", host_owner)
    else:
        state_store.write_apps_state(clear_credential_rejected(state_store.read_apps_state(), host_owner))
    updated = next(r for r in store.list_git_credentials() if r.host_owner == host_owner)
    logger.info("git-credential: set host=%s", host_owner)
    return GitCredentialRecordInfo(host_owner=updated.host_owner, updated_at=updated.updated_at)


@router.delete("/api/v1/git-credentials/{host_owner:path}")
def delete_git_credential(host_owner: str, store: SecretsStore = Depends(get_secrets_store)) -> None:
    """Remove a registered git credential. A no-op if none exists for ``host_owner``.

    Raises:
        PortalError: 422 ``invalid_host_owner`` if ``host_owner`` does not
            normalize to ``host/owner`` shape.
    """
    try:
        store.delete_git_credential(host_owner)
    except InvalidHostOwnerError as error:
        raise PortalError(422, "invalid_host_owner") from error
