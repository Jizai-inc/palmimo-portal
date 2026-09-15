"""Pure ledger transitions for the app tab: new job stamping and startup finalize.

Mirrors :mod:`palmimo_portal.core.update`'s split between pure state
transitions (here) and the side-effecting orchestration
(:mod:`palmimo_portal.core.apps_jobs`). Filesystem checks
(``manifest_exists``/``venv_exists``) are injected so this stays testable
with plain lambdas, the same shape as every other ``core/`` function here.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from palmimo_portal.core.secrets import normalize_host_owner
from palmimo_portal.ports import (
    AppJob,
    AppJobKind,
    AppRecord,
    AppsState,
    InvalidHostOwnerError,
    InvalidManifestSourceError,
)


def new_job_id() -> str:
    """Return a fresh job id, also used as the ``.staging/<id>/`` directory name.

    That directory name doubles as a ``palmimo-app-sync@<id>.service`` unit
    instance name (design doc 2.2b), which systemd only accepts matching
    ``^[a-z][a-z0-9-]{0,39}$`` -- the leading ``j`` guarantees a letter
    start even though a bare UUID hex digest can begin with a digit.
    """
    return f"j{uuid.uuid4().hex}"


#: A ref is a branch or tag name git accepts on a bare `--branch` flag, kept
#: narrow enough that it can never itself look like an option (no leading
#: `-`) or carry shell/argv metacharacters.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class InvalidGitSourceError(ValueError):
    """Raised by :func:`validate_git_url`/:func:`validate_git_ref`/:func:`validate_git_subdir_shape`.

    A plain :class:`ValueError` subclass so pydantic field validators can
    raise it directly and have FastAPI turn it into a 422 without any
    translation layer.
    """


def validate_git_url(url: str) -> str:
    """Reject anything that is not a plain ``https://<host>/...`` URL.

    Refusing userinfo (``https://user:pass@host/...``) and a leading ``-``
    closes the argv-injection hole a URL like ``--upload-pack=...`` would
    open once it lands right after git's own flags (see
    :mod:`palmimo_portal.adapters.git_port`); refusing anything but
    ``https`` closes off ``ext::``/``file://``/local-path transports that
    git treats as arbitrary command execution or arbitrary local reads.
    """
    if url.startswith("-"):
        raise InvalidGitSourceError("url must not start with '-'")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidGitSourceError("url must use https://")
    if not parsed.hostname:
        raise InvalidGitSourceError("url must name a host")
    if parsed.username or parsed.password:
        raise InvalidGitSourceError("url must not carry userinfo")
    return url


def validate_git_ref(ref: str) -> str:
    """Reject a ref that could be parsed as a git option or is otherwise not a plain branch/tag name."""
    if not _REF_RE.match(ref):
        raise InvalidGitSourceError("ref must match ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
    return ref


def validate_git_subdir_shape(subdir: str | None) -> str | None:
    """Reject a ``subdir`` that is not a plain relative POSIX path (no ``..``, not absolute).

    Cheap shape check only -- containment against the actual clone root is
    re-checked at each use site by :func:`resolve_subdir`, since that is
    the only place the root is known.
    """
    if subdir is None:
        return None
    pure = PurePosixPath(subdir)
    if pure.is_absolute() or ".." in pure.parts:
        raise InvalidGitSourceError("subdir must be a relative path with no '..' components")
    return subdir


def resolve_subdir(root: Path, subdir: str | None) -> Path:
    """Join ``subdir`` onto ``root`` and confirm the result stays inside it.

    ``subdir`` is untrusted (it rides in ``GitSourceRequest``/the ledger),
    so a value like ``../../etc`` must not walk the resulting path outside
    the clone it is supposed to be a part of, even though the API layer
    also rejects that shape on the way in -- this is the resolve-at-use-site
    half of that defense.

    Raises:
        InvalidManifestSourceError: ``subdir`` is absolute, escapes ``root``
            once resolved, or is not a plain relative POSIX path.
    """
    if not subdir:
        return root
    pure = PurePosixPath(subdir)
    if pure.is_absolute() or ".." in pure.parts:
        raise InvalidManifestSourceError(f"subdir {subdir!r} is not a relative path inside the project")
    resolved_root = root.resolve()
    candidate = (root / subdir).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise InvalidManifestSourceError(f"subdir {subdir!r} escapes the project root")
    return candidate


def host_owner_from_url(url: str) -> str | None:
    """Extract the normalized ``host/owner`` a git credential is scoped to (design doc 4.2) from a repo URL.

    Normalized through :func:`~palmimo_portal.core.secrets.normalize_host_owner`
    -- the same form every credential is stored/looked-up under, so this
    always lands on the right entry regardless of how the URL's host was
    cased. ``None`` for a URL with no host or no path segment (not a shape
    a real app source ever has, but a caller must not crash on one).
    """
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    try:
        return normalize_host_owner(f"{parsed.netloc}/{parts[0]}")
    except InvalidHostOwnerError:
        return None


def clear_credential_rejected(state: AppsState, host_owner: str) -> AppsState:
    """Un-mark every app whose git source is scoped to ``host_owner`` as no longer credential-rejected.

    Called when that credential is rewritten (design doc 3.6: a fresh PUT
    clears the mark) -- a no-op for any app that was not marked.
    """

    def _clear(record: AppRecord) -> AppRecord:
        if not record.credential_rejected or record.source.url is None:
            return record
        if host_owner_from_url(record.source.url) != host_owner:
            return record
        return replace(record, credential_rejected=False)

    return replace(state, apps={name: _clear(record) for name, record in state.apps.items()})


def new_app_job(kind: AppJobKind, now: float) -> AppJob:
    """Start a fresh, ``"running"`` :class:`AppJob`."""
    return AppJob(id=new_job_id(), kind=kind, state="running", step=None, error=None, started_at=now, finished_at=None)


def finalize_apps_state(
    state: AppsState,
    *,
    manifest_exists: Callable[[str], bool],
    venv_exists: Callable[[str], bool],
    now: float,
) -> AppsState:
    """Resolve a job orphaned by a process that died mid-job, and mark disk-inconsistent apps broken.

    Called once at Portal startup (design doc 3.3): a ``current_job`` still
    ``"running"`` means the process that started it never finished --
    stamped ``failed(interrupted)`` onto the app it targeted, same as
    :func:`~palmimo_portal.core.update.finalize_after_restart` does for the
    Portal's own update job. Every app is then re-checked against disk:
    missing ``palmimo.toml`` or ``.venv`` marks it ``broken(reason)``; an app
    that was broken but is no longer missing anything is un-marked.
    """
    apps = dict(state.apps)
    if state.current_job is not None and state.current_job.state == "running":
        target = state.current_job_app
        interrupted = replace(state.current_job, state="failed", error="interrupted", finished_at=now)
        if target is not None and target in apps:
            apps[target] = replace(apps[target], last_job=interrupted)

    reconciled = {name: _reconcile_broken(record, name, manifest_exists, venv_exists) for name, record in apps.items()}
    return AppsState(apps=reconciled, current_job=None, current_job_app=None)


def _reconcile_broken(
    record: AppRecord, name: str, manifest_exists: Callable[[str], bool], venv_exists: Callable[[str], bool]
) -> AppRecord:
    if not manifest_exists(name):
        return replace(record, broken_reason="manifest_missing")
    if not venv_exists(name):
        return replace(record, broken_reason="venv_missing")
    if record.broken_reason is not None:
        return replace(record, broken_reason=None)
    return record
