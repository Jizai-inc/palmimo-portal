"""Install / update / delete job pipelines for the app tab (design doc 3.4).

Each pipeline function runs one whole job to completion, calling an
``on_step`` callback (default a no-op) before each step so a caller can
persist progress -- see :class:`~palmimo_portal.core.apps_job_runner.AppsJobRunner`,
which runs these on a background thread under
:meth:`~palmimo_portal.ports.StateStore.lock_apps`, held for the whole
pipeline. Installing is split into :func:`prepare_install_zip`/
:func:`prepare_install_git` (fetch the source and read its manifest -- the
only way to learn the app's declared ``name``) and :func:`commit_install`
(dependency sync -> swap -> register, the unbounded step): the runner executes
``prepare_*`` synchronously in the request, so ``GET /apps`` can show
``installing`` against the right name immediately, and backgrounds only
``commit_install``.

Only the truly OS/environment-dependent steps (subprocess ``git``, the
``palmimo-app-sync@`` job unit, ``statvfs``) go through injected ports
(:class:`~palmimo_portal.ports.GitPort`, :class:`~palmimo_portal.ports.SyncUnitPort`,
:class:`~palmimo_portal.ports.DiskPort`); dependency sync itself runs as
``palmimo-app`` inside that unit, never in this process (design doc 2.2b) --
renaming and removing directories *within* the app tree this module already
owns (``apps/<id>/``, ``.staging/``, ``.trash/``) is done directly with
``pathlib``/``shutil`` -- there is no meaningful fake for "rename a
directory", and every such call is exercised against a real ``tmp_path`` in
tests, not mocked.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import shutil
import stat
import time
import tomllib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from palmimo_portal.core import apps as apps_core
from palmimo_portal.core.app_translations import MAX_TRANSLATIONS_BYTES, parse_translation_file
from palmimo_portal.core.apps import (
    app_namespace,
    host_owner_from_url,
    resolve_subdir,
    suggest_app_name,
)
from palmimo_portal.core.apps_layout import LayoutPaths, resolve_layout
from palmimo_portal.core.apps_zip import UPLOAD_MAX_BYTES, extract_zip_to_staging
from palmimo_portal.core.legacy_official import is_retired_official
from palmimo_portal.core.manifest import (
    DEFAULT_MANIFEST_FILENAME,
    Manifest,
    ManifestValidationError,
    manifest_from_snapshot,
    manifest_snapshot,
    parse_manifest,
    validate_asset_path,
    validate_manifest_filename,
)
from palmimo_portal.core.os_group import apps_gid
from palmimo_portal.core.public_catalog import (
    PublicAppMismatchError,
    PublicCatalogCache,
    is_public_source,
    public_app_status,
)
from palmimo_portal.core.secrets import mask_authorization_lines
from palmimo_portal.ports import (
    AppExistsError,
    AppJob,
    AppNotFoundError,
    AppRecord,
    AppRefKind,
    AppsDirUnavailableError,
    AppSource,
    AppsState,
    AppUnitPort,
    DiskFullError,
    DiskPort,
    GitPort,
    InvalidManifestSourceError,
    JournalPort,
    PublicCatalogApp,
    RunDirPort,
    SecretsStore,
    StateStore,
    SyncFailedError,
    SyncUnitPort,
    UvPort,
)


logger = logging.getLogger("palmimo_portal")


def _no_step(step: str) -> None:
    pass


def _no_record(record: AppRecord) -> None:
    pass


#: Reserved headroom beyond whatever a job is about to write, before a
#: precheck refuses with 507 `disk_full` (design doc 3.4).
INSTALL_DISK_RESERVE_BYTES = 500 * 1024 * 1024

#: Non-interactive, timeout-bounded git env (design doc 3.4): a stale or
#: revoked credential must fail fast, never hang on a prompt no one can answer.
_GIT_BASE_ENV: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"}

_PYPROJECT_FILENAME = "pyproject.toml"


@dataclass
class AppsJobContext:
    """Everything an install/update/delete job needs, gathered in one place."""

    apps_dir: Path
    uv_cache_dir: Path
    git: GitPort
    uv: UvPort
    sync_unit: SyncUnitPort
    journal: JournalPort
    disk: DiskPort
    secrets: SecretsStore
    now: Callable[[], float] = field(default=time.time)
    new_id: Callable[[], str] = field(default=apps_core.new_job_id)
    #: Only needed for :func:`_ensure_not_running_before_swap`'s update/delete TOCTOU recheck
    #: (design doc 3.5) -- ``None`` in tests that never exercise an update/delete swap skips
    #: the recheck rather than requiring every caller to wire a state store and app-unit port.
    app_unit: AppUnitPort | None = None
    state: StateStore | None = None
    #: Only needed by :func:`purge_app_files` to remove a deleted app's run directory --
    #: ``None`` in tests that never exercise delete skips that cleanup.
    run_dir: RunDirPort | None = None
    public_catalog_cache: PublicCatalogCache | None = None
    public_catalog_url: str | None = None

    @property
    def staging_dir(self) -> Path:
        return self.apps_dir / ".staging"

    @property
    def trash_dir(self) -> Path:
        return self.apps_dir / ".trash"

    def app_dir(self, name: str) -> Path:
        return self.apps_dir / name


#: `TimeoutStartSec` on `palmimo-app-sync@.service` (design doc 2.2b) -- the wait budget matches.
SYNC_TIMEOUT_SECONDS = 1800.0

_SYNC_JOURNAL_TAIL_LINES = 20
_SYNC_UNIT_TEMPLATE = "palmimo-app-sync@{name}.service"


def sync_unit_name(instance: str) -> str:
    """Return the ``palmimo-app-sync@<instance>.service`` unit string for a staging instance."""
    return _SYNC_UNIT_TEMPLATE.format(name=instance)


def _prepare_staging_for_sync(staging_container: Path) -> None:
    """Set ``user:palmimo-apps`` group ownership and setgid ``2775`` so ``palmimo-app`` can write here.

    Skips silently when the ``palmimo-apps`` group does not resolve on this
    host (design doc 2.2b) -- a dev machine with no such group configured.
    """
    gid = apps_gid()
    if gid is not None:
        with contextlib.suppress(OSError):
            os.chown(staging_container, -1, gid)
    with contextlib.suppress(OSError):
        staging_container.chmod(stat.S_ISGID | 0o775)


def _lock_exists(project: Path, clone_root: Path) -> bool:
    """Whether ``project`` or an ancestor up to ``clone_root`` already has a ``uv.lock``.

    uv resolves a workspace member against the lock at the workspace root,
    which sits above ``project`` when ``clone_root`` turns out to be a
    workspace and ``project`` one of its members.
    """
    candidate = project.resolve()
    root = clone_root.resolve()
    while True:
        if (candidate / "uv.lock").is_file():
            return True
        if candidate == root:
            return False
        candidate = candidate.parent


def _sync_dependencies(
    ctx: AppsJobContext,
    instance: str,
    app_id: str,
    staging_container: Path,
    layout: LayoutPaths,
    clone_root: Path,
    requires_python: str | None,
) -> bool:
    """Sync ``layout.project``'s dependencies through the ``palmimo-app-sync@<instance>`` unit (design doc 2.2b).

    Runs as ``palmimo-app``, never the Portal's own uid -- an untrusted
    ``pyproject.toml``'s build backend or path dependencies must not run
    with the Portal's privileges. Returns whether no ``uv.lock`` existed
    yet (the helper then generates one).

    Swaps ``uv_cache_dir/<app_id>`` into the staging tree as ``.uv-cache``
    for the duration of the sync and back out afterward (design doc 3.10) --
    the cache is per app id so one app's sync can never read wheels another
    app's build backend placed there.

    Raises:
        SyncFailedError: the unit did not finish successfully.
    """
    if requires_python is not None and ctx.uv.find_system_python(requires_python) is None:
        ctx.uv.install_python(requires_python)

    persistent_cache = ctx.uv_cache_dir / app_id
    staged_cache = staging_container / ".uv-cache"
    if staged_cache.exists() or staged_cache.is_symlink():
        raise InvalidManifestSourceError(f"app source reserves internal cache path: {staged_cache}")
    ctx.uv_cache_dir.mkdir(parents=True, exist_ok=True)
    if persistent_cache.is_symlink():
        raise InvalidManifestSourceError(f"app uv cache is a symlink: {persistent_cache}")
    if persistent_cache.exists() and not persistent_cache.is_dir():
        raise InvalidManifestSourceError(f"app uv cache is not a directory: {persistent_cache}")
    persistent_cache.mkdir(parents=True, exist_ok=True)
    _prepare_cache_for_sync(persistent_cache)
    persistent_cache.rename(staged_cache)
    _prepare_cache_for_sync(staged_cache)
    try:
        result = _run_sync_dependencies(ctx, instance, staging_container, layout, clone_root)
    except SyncUnitContainmentError:
        # The unit may still be writing into the staged cache; it stays with the staging tree.
        raise
    except BaseException:
        # A raise here (e.g. from rename()) replaces a real SyncFailedError from the try block above.
        _restore_persistent_cache(staged_cache, persistent_cache)
        raise
    _restore_persistent_cache(staged_cache, persistent_cache)
    return result


def _restore_persistent_cache(staged_cache: Path, persistent_cache: Path) -> None:
    if not staged_cache.exists():
        persistent_cache.mkdir(parents=True, exist_ok=True)
        return
    if persistent_cache.exists():
        logger.warning("app uv cache reappeared during sync; discarding stale copy at %s", persistent_cache)
        shutil.rmtree(persistent_cache, ignore_errors=True)
    staged_cache.rename(persistent_cache)


def _prepare_cache_for_sync(path: Path) -> None:
    gid = apps_gid()
    if gid is not None:
        with contextlib.suppress(OSError):
            os.chown(path, -1, gid)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_ISGID | 0o775)


def _write_reserved_json(path: Path, data: object) -> None:
    """Write ``data`` as JSON to ``path``, refusing to follow or replace anything already there.

    ``path`` sits inside a fetched (possibly untrusted) tree -- a repository
    could place its own file, or a symlink, exactly where Portal is about to
    write. ``O_EXCL`` refuses any existing entry outright; ``O_NOFOLLOW`` is
    defense in depth against a symlink swapped in between the check and the
    open.

    Raises:
        InvalidManifestSourceError: ``path`` already exists.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as error:
        raise InvalidManifestSourceError(f"app source reserves internal path: {path}") from error
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))


class SyncUnitContainmentError(Exception):
    """Raised when a failed sync unit could not be confirmed inactive."""


def _contain_sync_unit(ctx: AppsJobContext, instance: str, cause: Exception) -> None:
    try:
        ctx.sync_unit.stop(instance)
        still_active = instance in ctx.sync_unit.list_active_instances()
    except Exception as error:
        _log_uncontained(instance, str(error))
        raise SyncUnitContainmentError(f"could not stop sync unit {instance}: {error}") from cause
    if still_active:
        _log_uncontained(instance, "still active after stop")
        raise SyncUnitContainmentError(f"sync unit {instance} is still active") from cause


def _log_uncontained(instance: str, reason: str) -> None:
    logger.error(
        "apps: sync unit instance=%s could not be confirmed stopped (%s); "
        "app jobs and app starts stay blocked until the Portal restarts",
        instance,
        reason,
    )


def _run_sync_dependencies(
    ctx: AppsJobContext,
    instance: str,
    staging_container: Path,
    layout: LayoutPaths,
    clone_root: Path,
) -> bool:
    """Run untrusted project dependency hooks inside the restricted sync unit.

    ``sync.json`` carries no ``requires-python`` -- the on-device ``app-sync``
    helper (palmimo-image) reads it straight from the project's own
    ``pyproject.toml`` rather than trusting a value from Portal. The
    interpreter search/install (design doc 3.10, shared Python) still
    happens here, before the unit starts, so the sync unit itself never
    needs the requirement passed to it.
    """
    _prepare_staging_for_sync(staging_container)
    lock_generated = not _lock_exists(layout.project, clone_root)
    sync_spec = {
        "project": str(layout.project),
        "frozen": not lock_generated,
        "relocatable": True,
    }
    _write_reserved_json(staging_container / "sync.json", sync_spec)
    ctx.sync_unit.start(instance)
    contained = False
    try:
        status = ctx.sync_unit.wait(instance, timeout_s=SYNC_TIMEOUT_SECONDS)
        contained = True
    except Exception as error:
        _contain_sync_unit(ctx, instance, error)
        contained = True
        raise
    finally:
        if contained:
            (staging_container / "sync.json").unlink(missing_ok=True)
    if status.result != "success" or status.exec_main_status != 0:
        raise SyncFailedError(instance, status.result, status.exec_main_status, _sync_journal_tail(ctx, instance))
    return lock_generated


def _sync_journal_tail(ctx: AppsJobContext, instance: str) -> str:
    page = ctx.journal.read(unit=sync_unit_name(instance), cursor=None, lines=_SYNC_JOURNAL_TAIL_LINES)
    return mask_authorization_lines("\n".join(entry.message for entry in page.entries))


def git_env(ctx: AppsJobContext, url: str) -> dict[str, str]:
    """Build the git subprocess env for ``url``, injecting a registered credential if one matches.

    Never puts the token in argv (design doc 4.2) -- ``GIT_CONFIG_VALUE_0``
    carries it as an ``Authorization`` header value instead, scoped to the
    URL's own host via ``GIT_CONFIG_KEY_0``.
    """
    env = dict(_GIT_BASE_ENV)
    host_owner = host_owner_from_url(url)
    if host_owner is None:
        return env
    token = ctx.secrets.get_git_credential(host_owner)
    if token is None:
        return env
    parsed = urlparse(url)
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"http.https://{parsed.netloc}/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {encoded}"
    return env


def _check_disk_space(ctx: AppsJobContext, extra_bytes: int) -> None:
    required = extra_bytes + INSTALL_DISK_RESERVE_BYTES
    try:
        free = ctx.disk.free_bytes(ctx.apps_dir)
    except OSError as error:
        raise AppsDirUnavailableError(str(error)) from error
    if free < required:
        raise DiskFullError(f"{free} bytes free, need at least {required} bytes")


def _read_manifest(project_dir: Path, manifest_filename: str = DEFAULT_MANIFEST_FILENAME) -> Manifest:
    manifest_path = project_dir / manifest_filename
    if not manifest_path.is_file():
        raise InvalidManifestSourceError(f"{manifest_filename} not found in {project_dir}")

    def read_include(path: str) -> str:
        try:
            with _asset_source_path(project_dir, path).open("rb") as stream:
                content = stream.read(65_537)
            if len(content) > 65_536:
                raise ValueError("asset include exceeds 64 KiB")
            return content.decode("utf-8")
        except (OSError, ValueError) as error:
            raise ManifestValidationError([f"invalid asset include: {error}"]) from error

    manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"), read_include=read_include)
    for asset in manifest.assets.values():
        _asset_source_path(project_dir, asset.file)
    return replace(manifest, translations=_read_translations(project_dir, manifest_filename))


def _asset_source_path(project_dir: Path, path: str) -> Path:
    try:
        validate_asset_path(path)
        candidate = project_dir.resolve()
        for part in Path(path).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("asset paths cannot follow symlinks")
        if not candidate.is_file():
            raise ValueError("asset source must be an existing regular file")
        return candidate
    except (OSError, ValueError) as error:
        raise ManifestValidationError([f"invalid asset source {path!r}: {error}"]) from error


def _read_translations(project_dir: Path, manifest_filename: str) -> dict[str, dict[str, str]]:
    translation_path = project_dir / f"{manifest_filename}.i18n.json"
    try:
        if translation_path.is_symlink() or not translation_path.is_file():
            return {}
        with translation_path.open("rb") as stream:
            content = stream.read(MAX_TRANSLATIONS_BYTES + 1)
        if len(content) > MAX_TRANSLATIONS_BYTES:
            return {}
        return parse_translation_file(content.decode("utf-8"))
    except (OSError, UnicodeError):
        return {}


def _fetch_git_source(
    ctx: AppsJobContext,
    *,
    url: str,
    ref: str,
    ref_kind: AppRefKind,
    subdir: str | None,
    manifest: str | None,
    dest: Path,
) -> str:
    return ctx.git.clone_shallow(url, ref, ref_kind, dest, env=git_env(ctx, url))


def _manifest_filename_for(source: AppSource) -> str:
    return source.manifest or DEFAULT_MANIFEST_FILENAME


def _stored_manifest(resolved_manifest_filename: str) -> str | None:
    """The value an :class:`AppSource` stores for a resolved manifest filename -- ``None`` for the default."""
    return None if resolved_manifest_filename == DEFAULT_MANIFEST_FILENAME else resolved_manifest_filename


def _check_pyproject(project_dir: Path) -> None:
    if not (project_dir / _PYPROJECT_FILENAME).is_file():
        raise InvalidManifestSourceError(f"{_PYPROJECT_FILENAME} not found in {project_dir}")


def _read_requires_python(project_dir: Path) -> str | None:
    try:
        data = tomllib.loads((project_dir / _PYPROJECT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    requirement = project.get("requires-python")
    return requirement if isinstance(requirement, str) else None


def _chmod_group_rwx(root: Path) -> None:
    """Set ``g+rwX`` (dirs also setgid, i.e. ``2775``) recursively on the Portal-owned fetched tree.

    Runs *before* the ``palmimo-app-sync@`` unit is started, never after
    (design doc 2.2b): sync writes files as ``palmimo-app``, and a chmod
    afterward would ``EPERM`` on anything it created. Setgid on every
    directory means a file the unit creates under ``root`` also lands in
    the ``palmimo-apps`` group without a separate recursive chown.
    Rejects any symlink the same way :mod:`~palmimo_portal.core.apps_zip`
    already rejects one in a zip upload: ``Path.chmod``'s default
    ``follow_symlinks=True`` would silently chmod whatever arbitrary path
    the symlink's target names, which a git checkout (unlike a validated
    zip) is under no obligation to keep inside the clone.

    Raises:
        InvalidManifestSourceError: ``root`` or any entry under it is a symlink.
    """
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise InvalidManifestSourceError(f"app source contains a symlink: {path}")
        mode = path.stat().st_mode
        extra = stat.S_IRGRP | stat.S_IWGRP
        if mode & stat.S_IXUSR or path.is_dir():
            extra |= stat.S_IXGRP
        if path.is_dir():
            extra |= stat.S_ISGID
        path.chmod(mode | extra, follow_symlinks=False)


class AppRunningAtSwapError(Exception):
    """Raised when a start slipped in between an update/delete job's own prechecks and its swap.

    :func:`~palmimo_portal.core.apps_start.start_app` refuses 409
    ``app_job_in_progress`` for *this* app while a job is recorded against
    it, but the job itself only learned that at the start of its own
    (possibly long) fetch/sync -- :func:`_ensure_not_running_before_swap`
    re-checks immediately before the rename that would touch a running
    app's files, under ``run.lock`` so a start cannot interleave between
    the check and the rename. ``str(error)`` is ``"app_running"``, the job
    ``error`` field a caller persists as-is.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__("app_running")


def _ensure_not_running_before_swap(ctx: AppsJobContext, name: str) -> AbstractContextManager[None]:
    """Acquire ``run.lock`` and confirm ``name``'s unit is not active, held through the caller's swap.

    A no-op (returns a null context manager) when ``ctx.app_unit``/``ctx.state``
    are unset. Raises :class:`AppRunningAtSwapError` if occupied, releasing
    the lock first.

    Raises:
        AppRunningAtSwapError: ``name``'s unit is active/activating/deactivating.
        RunLockTimeoutError: ``run.lock`` is already held (a start/stop/autostart in flight).
    """
    if ctx.app_unit is None or ctx.state is None:
        return contextlib.nullcontext()
    from palmimo_portal.core.apps_start import RUNNING_ACTIVE_STATES

    lock_cm = ctx.state.lock_run()
    lock_cm.__enter__()
    try:
        if ctx.app_unit.status(name).active_state in RUNNING_ACTIVE_STATES:
            raise AppRunningAtSwapError(name)
    except BaseException:
        lock_cm.__exit__(None, None, None)
        raise
    return lock_cm


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        errors: list[OSError] = []

        def onerror(function: Callable[..., object], name: str, error: tuple[object, object, object]) -> None:
            try:
                Path(name).chmod(0o770)
                function(name)
            except OSError as retry_error:
                errors.append(retry_error)

        shutil.rmtree(path, onerror=onerror)
        if path.exists() or path.is_symlink():
            raise errors[0] if errors else OSError(f"could not remove {path}")
    else:
        path.unlink()


def purge_path(ctx: AppsJobContext, path: Path) -> str | None:
    """Remove ``path`` (a file or directory under ``apps_dir``/``uv_cache_dir``), escalating to
    the ``palmimo-app-sync@`` unit's "purge mode" if a plain removal fails -- e.g. a file
    ``palmimo-app`` owns from a previous sync that Portal's own uid cannot delete.

    The unit's ``instance`` must be a ``.staging/<id>/`` directory name (systemd only accepts
    ``^[a-z][a-z0-9-]{0,39}$``, and the on-device helper reads
    ``.staging/<instance>/sync.json`` -- see ``app-sync`` in palmimo-image), never ``path``
    itself: this purges a stuck path *inside* ``.trash``/``.staging`` that Portal cannot write
    into, so the request has to live in a fresh staging directory alongside it, not in the path
    being removed. That staging directory is Portal-owned throughout (the unit only removes
    ``path``, per the ``app-sync`` purge contract) and is cleaned up here once the unit is done.

    Returns ``path`` (as a string) if it is still present afterward, or ``None`` once
    confirmed gone.

    Raises:
        ValueError: ``path`` is not a direct child of ``.staging/`` or
            ``.trash/`` -- mirrors the on-device ``app-sync`` helper's own
            purge-mode contract (palmimo-image), which refuses anything
            else (including the bare ``.staging``/``.trash`` roots) with
            exit 64. A caller wanting to purge something elsewhere (an
            installed app's directory, a `uv_cache_dir` entry) must first
            rename it into ``.trash/<uuid>/`` on the same filesystem.
    """
    if path.parent not in (ctx.staging_dir, ctx.trash_dir):
        raise ValueError(f"refusing to purge a path outside .staging/.trash: {path}")
    if not path.exists() and not path.is_symlink():
        return None
    try:
        _remove(path)
        return None
    except OSError:
        pass

    if path.parent == ctx.staging_dir:
        moved_path = ctx.trash_dir / ctx.new_id()
        try:
            ctx.trash_dir.mkdir(parents=True, exist_ok=True)
            path.rename(moved_path)
        except OSError as error:
            logger.warning("apps: could not move staging path into trash path=%s: %s", path, error)
            return str(path)
        path = moved_path

    instance = f"purge-{ctx.new_id()}"
    staging_container = ctx.staging_dir / instance
    staging_container.mkdir(parents=True, exist_ok=True)
    _prepare_staging_for_sync(staging_container)
    contained = False
    try:
        _write_reserved_json(staging_container / "sync.json", {"purge": str(path)})
        ctx.sync_unit.start(instance)
        ctx.sync_unit.wait(instance, timeout_s=SYNC_TIMEOUT_SECONDS)
        contained = True
    except Exception as error:
        try:
            ctx.sync_unit.stop(instance)
        except Exception as stop_error:
            logger.warning("apps: could not stop failed purge unit instance=%s: %s", instance, stop_error)
        else:
            contained = True
        logger.warning("apps: purge request failed path=%s: %s", path, error)
    finally:
        if contained:
            shutil.rmtree(staging_container, ignore_errors=True)

    if path.exists() or path.is_symlink():
        logger.warning("apps: purge left a leftover path=%s", path)
        return str(path)
    return None


def read_manifest_for_app(ctx: AppsJobContext, record: AppRecord) -> Manifest:
    """Return the validated manifest captured when this app was installed or updated."""
    if record.manifest is not None:
        manifest = manifest_from_snapshot(record.manifest)
        try:
            project_dir = resolve_subdir(ctx.app_dir(record.id), record.source.subdir)
        except InvalidManifestSourceError:
            return manifest
        return replace(manifest, translations=_read_translations(project_dir, _manifest_filename_for(record.source)))
    # Persisted legacy ledgers are rejected by the state adapter.
    project_dir = resolve_subdir(ctx.app_dir(record.id), record.source.subdir)
    return _read_manifest(project_dir, _manifest_filename_for(record.source))


def _upload_size(upload: bytes | Path) -> int:
    return len(upload) if isinstance(upload, bytes) else upload.stat().st_size


def preview_zip(ctx: AppsJobContext, upload: bytes | Path, manifest_filename: str | None = None) -> Manifest:
    """Fetch, extract, and validate a zip upload without installing it. Discards the staging tree.

    ``upload`` as a :class:`Path` (already streamed to disk by the API
    layer -- design doc 3.4) is read from there instead of held in memory.
    """
    if _upload_size(upload) > UPLOAD_MAX_BYTES:
        raise InvalidManifestSourceError(f"upload exceeds the {UPLOAD_MAX_BYTES // (1024 * 1024)} MB size cap")
    resolved_manifest = validate_manifest_filename(manifest_filename)
    staging_container = ctx.staging_dir / ctx.new_id()
    try:
        install_root = extract_zip_to_staging(upload, staging_container, manifest_filename=resolved_manifest)
        manifest = _read_manifest(install_root, resolved_manifest)
        _check_pyproject(install_root)
        return manifest
    finally:
        purge_path(ctx, staging_container)


def preview_git(
    ctx: AppsJobContext,
    *,
    url: str,
    ref: str,
    ref_kind: AppRefKind,
    subdir: str | None = None,
    manifest_filename: str | None = None,
) -> Manifest:
    """Shallow-clone, then validate, a git source without installing it. Discards the staging tree."""
    resolved_manifest = validate_manifest_filename(manifest_filename)
    staging_container = ctx.staging_dir / ctx.new_id()
    try:
        _fetch_git_source(
            ctx,
            url=url,
            ref=ref,
            ref_kind=ref_kind,
            subdir=subdir,
            manifest=_stored_manifest(resolved_manifest),
            dest=staging_container,
        )
        project_dir = resolve_subdir(staging_container, subdir)
        manifest = _read_manifest(project_dir, resolved_manifest)
        _check_pyproject(project_dir)
        return manifest
    finally:
        purge_path(ctx, staging_container)


@dataclass(frozen=True)
class PreparedInstall:
    """The fast, synchronous half of an install: a fetched source with its manifest already read.

    Returned by :func:`prepare_install_zip`/:func:`prepare_install_git` so a
    caller learns the app's declared ``name`` (and can reject an
    already-installed one, or show ``installing`` against the right name)
    before handing the slow half (:func:`commit_install`) to a background thread.
    """

    job_id: str
    manifest: Manifest
    install_root: Path
    project_dir: Path
    staging_container: Path
    source: AppSource
    commit: str | None
    requires_python: str | None = None
    app_id: str | None = None
    public_app: PublicCatalogApp | None = None


def prepare_install_zip(
    ctx: AppsJobContext, upload: bytes | Path, job_id: str | None = None, manifest_filename: str | None = None
) -> PreparedInstall:
    """Fetch, extract, and validate a zip upload -- the fast half of :func:`install_zip`.

    ``upload`` as a :class:`Path` (already streamed to disk by the API
    layer -- design doc 3.4) is read from there instead of held in memory.

    Raises:
        InvalidManifestSourceError: the upload is oversized or fails
            extraction/validation (see :mod:`palmimo_portal.core.apps_zip`).
        DiskFullError: not enough free space for the upload plus the reserve.
    """
    upload_size = _upload_size(upload)
    if upload_size > UPLOAD_MAX_BYTES:
        raise InvalidManifestSourceError(f"upload exceeds the {UPLOAD_MAX_BYTES // (1024 * 1024)} MB size cap")
    resolved_manifest = validate_manifest_filename(manifest_filename)
    _check_disk_space(ctx, upload_size)
    job_id = job_id or ctx.new_id()
    staging_container = ctx.staging_dir / job_id
    try:
        install_root = extract_zip_to_staging(upload, staging_container, manifest_filename=resolved_manifest)
        manifest = _read_manifest(install_root, resolved_manifest)
        _check_pyproject(install_root)
    except BaseException:
        purge_path(ctx, staging_container)
        raise
    return PreparedInstall(
        job_id=job_id,
        manifest=manifest,
        install_root=install_root,
        project_dir=install_root,
        staging_container=staging_container,
        source=AppSource(type="zip", manifest=_stored_manifest(resolved_manifest)),
        commit=None,
        requires_python=_read_requires_python(install_root),
    )


def prepare_install_git(
    ctx: AppsJobContext,
    *,
    url: str,
    ref: str,
    ref_kind: AppRefKind,
    subdir: str | None = None,
    job_id: str | None = None,
    manifest_filename: str | None = None,
) -> PreparedInstall:
    """Shallow-clone and validate a git source -- the fast half of :func:`install_git`.

    Raises:
        DiskFullError: not enough free space for the reserve.
        InvalidManifestSourceError / ManifestValidationError: the clone
            fails validation.
    """
    return _prepare_install_git_source(
        ctx,
        AppSource(
            type="git",
            url=url,
            ref=ref,
            ref_kind=ref_kind,
            subdir=subdir,
            manifest=_stored_manifest(validate_manifest_filename(manifest_filename)),
        ),
        job_id,
    )


def prepare_install_public(ctx: AppsJobContext, app: PublicCatalogApp) -> PreparedInstall:
    source = replace(app.source, public_app_id=app.public.app_id)
    prepared = _prepare_install_git_source(ctx, source, None)
    try:
        if set(prepared.manifest.devices) != set(app.devices) or {
            name: spec.required for name, spec in prepared.manifest.env.items()
        } != {name: spec.required for name, spec in app.env.items()}:
            raise PublicAppMismatchError("public_app_mismatch")
    except BaseException:
        purge_path(ctx, prepared.staging_container)
        raise
    return replace(prepared, public_app=app)


def _clone_app_source(ctx: AppsJobContext, source: AppSource, dest: Path) -> str:
    assert source.url is not None and source.ref is not None and source.ref_kind is not None
    if source.public_app_id:
        assert source.commit is not None
        return ctx.git.clone_commit_shallow(source.url, source.commit, dest)
    return _fetch_git_source(
        ctx,
        url=source.url,
        ref=source.ref,
        ref_kind=source.ref_kind,
        subdir=source.subdir,
        manifest=source.manifest,
        dest=dest,
    )


def _prepare_install_git_source(ctx: AppsJobContext, source: AppSource, job_id: str | None) -> PreparedInstall:
    resolved_manifest = _manifest_filename_for(source)
    _check_disk_space(ctx, 0)
    job_id = job_id or ctx.new_id()
    staging_container = ctx.staging_dir / job_id
    try:
        commit = _clone_app_source(ctx, source, staging_container)
        project_dir = resolve_subdir(staging_container, source.subdir)
        manifest = _read_manifest(project_dir, resolved_manifest)
        _check_pyproject(project_dir)
    except BaseException:
        purge_path(ctx, staging_container)
        raise
    return PreparedInstall(
        job_id=job_id,
        manifest=manifest,
        install_root=staging_container,
        project_dir=project_dir,
        staging_container=staging_container,
        source=source,
        commit=commit,
        requires_python=_read_requires_python(project_dir),
    )


def commit_install(
    ctx: AppsJobContext,
    state: AppsState,
    prepared: PreparedInstall,
    started: float,
    on_step: Callable[[str], None] = _no_step,
) -> tuple[AppsState, AppRecord]:
    """Sync, swap, and register a :class:`PreparedInstall` -- the slow half, safe to run in the background.

    Raises:
        AppExistsError: ``prepared.app_id`` is already installed.
    """
    app_id = prepared.app_id
    if app_id is None and prepared.public_app is not None:
        app_id = apps_core.app_id_from_public_catalog(prepared.public_app, state)
    if app_id is None:
        namespace = app_namespace(prepared.source.type, prepared.source.url)
        app_id = f"{namespace}.{suggest_app_name(namespace, prepared.manifest.name, state)}"
    if app_id in state.apps:
        raise AppExistsError(app_id)
    leftover_cache = _purge_uv_cache(ctx, app_id)
    if leftover_cache is not None:
        raise OSError(f"could not remove stale uv cache: {leftover_cache}")
    on_step("sync")
    layout = resolve_layout(prepared.install_root, prepared.source.subdir)
    _chmod_group_rwx(prepared.project_dir)
    lock_generated = _sync_dependencies(
        ctx,
        prepared.job_id,
        app_id,
        prepared.staging_container,
        layout,
        prepared.install_root,
        prepared.requires_python,
    )
    on_step("swap")
    dest = ctx.app_dir(app_id)
    prepared.install_root.rename(dest)
    on_step("register")
    job = AppJob(
        id=prepared.job_id,
        kind="install",
        state="done",
        step="register",
        error=None,
        started_at=started,
        finished_at=ctx.now(),
        display_name=prepared.manifest.name,
        lock_generated=lock_generated,
    )
    record = AppRecord(
        name=prepared.manifest.name,
        source=replace(prepared.source, commit=prepared.commit),
        installed_at=ctx.now(),
        params={},
        autostart=False,
        last_job=job,
        manifest=manifest_snapshot(prepared.manifest),
        requires_python=prepared.requires_python,
        id=app_id,
    )
    new_state = AppsState(apps={**state.apps, app_id: record})
    return new_state, record


def install_zip(
    ctx: AppsJobContext,
    state: AppsState,
    upload: bytes,
    manifest_filename: str | None = None,
    on_step: Callable[[str], None] = _no_step,
) -> tuple[AppsState, AppRecord]:
    """Install an app from a zip upload, synchronously: :func:`prepare_install_zip` then :func:`commit_install`."""
    started = ctx.now()
    on_step("fetch")
    prepared = prepare_install_zip(ctx, upload, manifest_filename=manifest_filename)
    try:
        on_step("validate")
        return commit_install(ctx, state, prepared, started, on_step)
    finally:
        purge_path(ctx, prepared.staging_container)


def install_git(
    ctx: AppsJobContext,
    state: AppsState,
    *,
    url: str,
    ref: str,
    ref_kind: AppRefKind,
    subdir: str | None = None,
    manifest_filename: str | None = None,
    on_step: Callable[[str], None] = _no_step,
) -> tuple[AppsState, AppRecord]:
    """Install an app from a git source, synchronously: :func:`prepare_install_git` then :func:`commit_install`."""
    started = ctx.now()
    on_step("fetch")
    prepared = prepare_install_git(
        ctx, url=url, ref=ref, ref_kind=ref_kind, subdir=subdir, manifest_filename=manifest_filename
    )
    try:
        on_step("validate")
        return commit_install(ctx, state, prepared, started, on_step)
    finally:
        # A no-op once `install_root.rename(dest)` has already moved this
        # path away -- `shutil.rmtree` on a missing path is not an error.
        purge_path(ctx, prepared.staging_container)


def update_git(
    ctx: AppsJobContext,
    state: AppsState,
    name: str,
    *,
    job_id: str | None = None,
    started: float | None = None,
    on_step: Callable[[str], None] = _no_step,
    on_registered: Callable[[AppRecord], None] = _no_record,
    prepared: PreparedInstall | None = None,
) -> tuple[AppsState, AppRecord]:
    """Stage the selected git version and swap it in after dependency sync.

    Sync runs on the staged clone, *before* the swap -- a failure here
    never touches the live tree (design doc 3.4: in-place update was
    rejected precisely because a failure mid-sync would leave the running
    ``.venv`` half-written).

    Raises:
        AppNotFoundError: no app named ``name`` is installed.
        InvalidManifestSourceError: ``name`` is not git-sourced.
        DiskFullError: not enough free space for the reserve.
    """
    record = state.apps.get(name)
    if record is None:
        raise AppNotFoundError(name)
    if record.source.type != "git":
        raise InvalidManifestSourceError(f"app {name!r} is not git-sourced; update is git-only")
    assert record.source.url is not None and record.source.ref is not None and record.source.ref_kind is not None

    _check_disk_space(ctx, 0)
    job_id = job_id or ctx.new_id()
    started = started if started is not None else ctx.now()
    staging_container = ctx.staging_dir / job_id
    retired = is_retired_official(record)
    if is_public_source(record) and not retired and prepared is None:
        raise ValueError("public updates require a confirmed prepared source")
    if prepared is not None:
        staging_container = prepared.staging_container
        target_source = prepared.source
    else:
        target_source = record.source
    uncontained = False
    try:
        on_step("fetch")
        if prepared is not None:
            commit = prepared.commit
        elif retired:
            if target_source.commit is None:
                raise InvalidManifestSourceError(f"app {name!r} has no recorded commit")
            commit = ctx.git.clone_commit_shallow(record.source.url, target_source.commit, staging_container)
        else:
            commit = _clone_app_source(ctx, target_source, staging_container)
        on_step("validate")
        project_dir = resolve_subdir(staging_container, target_source.subdir)
        manifest = _read_manifest(project_dir, _manifest_filename_for(target_source))
        _check_pyproject(project_dir)
        requires_python = _read_requires_python(project_dir)
        _chmod_group_rwx(project_dir)
        on_step("sync")
        layout = resolve_layout(staging_container, target_source.subdir)
        lock_generated = _sync_dependencies(
            ctx, job_id, record.id, staging_container, layout, staging_container, requires_python
        )

        # Computed before the swap (the dropped set depends on the new manifest, fetched
        # above) but only ever written at "register", after a successful swap -- a failed
        # swap must leave the existing bindings completely intact, not partially dropped
        # for an app whose old tree (and old manifest) is still the one running.
        dropped_bindings, dropped_params, kept_bindings = _reconcile_dropped(ctx, name, manifest, record.params)

        on_step("swap")
        swap_lock = _ensure_not_running_before_swap(ctx, name)
        try:
            dest = ctx.app_dir(name)
            trash = ctx.trash_dir / job_id
            if dest.exists():
                ctx.trash_dir.mkdir(parents=True, exist_ok=True)
                dest.rename(trash)
            staging_container.rename(dest)
        finally:
            swap_lock.__exit__(None, None, None)
        on_step("register")
        if dropped_bindings:
            ctx.secrets.write_bindings(name, kept_bindings)
            for request_name in dropped_bindings:
                logger.info("apps: binding dropped app=%s req=%s", name, request_name)
        for param_name in dropped_params:
            logger.info("apps: param dropped app=%s name=%s", name, param_name)
        if manifest.name != record.name:
            # The id stays put (design doc 3.9): only the ledger's display name follows a
            # rename, so an app the operator is already running/binding secrets against
            # never has to be reinstalled just because its author renamed it upstream.
            logger.info("apps: app renamed id=%s old=%s new=%s", record.id, record.name, manifest.name)
        job = AppJob(
            id=job_id,
            kind="update",
            state="done",
            step="register",
            error=None,
            started_at=started,
            finished_at=ctx.now(),
            lock_generated=lock_generated,
            dropped_bindings=dropped_bindings,
            dropped_params=dropped_params,
        )
        new_record = AppRecord(
            name=manifest.name,
            source=replace(target_source, commit=commit),
            installed_at=ctx.now(),
            params={key: value for key, value in record.params.items() if key not in dropped_params},
            autostart=record.autostart,
            last_job=job,
            manifest=manifest_snapshot(manifest),
            requires_python=requires_python,
            id=record.id,
        )
        on_registered(new_record)
        leftover = purge_path(ctx, trash)
        if leftover is not None:
            new_record = replace(
                new_record,
                last_job=replace(job, error=f"could not remove old app files at {leftover}"),
            )
            on_registered(new_record)
        new_state = AppsState(apps={**state.apps, name: new_record})
        return new_state, new_record
    except SyncUnitContainmentError:
        # The unit may still be using the staging tree; the startup sweep removes it after
        # stopping the unit.
        uncontained = True
        raise
    finally:
        if not uncontained:
            purge_path(ctx, staging_container)


def _reconcile_dropped(
    ctx: AppsJobContext, name: str, manifest: Manifest, params: dict
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """Compute (never write) which bindings/params the new manifest no longer declares."""
    bindings = ctx.secrets.read_bindings(name)
    dropped_bindings = tuple(sorted(request_name for request_name in bindings if request_name not in manifest.env))
    kept_bindings = {key: value for key, value in bindings.items() if key not in dropped_bindings}
    dropped_params = tuple(sorted(param_name for param_name in params if param_name not in manifest.params))
    return dropped_bindings, dropped_params, kept_bindings


def delete_from_ledger(state: AppsState, name: str) -> tuple[AppsState, AppRecord]:
    """First step of delete (design doc 3.4): drop ``name`` from the ledger, before touching its files.

    Raises:
        AppNotFoundError: no app named ``name`` is installed.
    """
    record = state.apps.get(name)
    if record is None:
        raise AppNotFoundError(name)
    remaining = {key: value for key, value in state.apps.items() if key != name}
    return AppsState(apps=remaining), record


def purge_app_files(ctx: AppsJobContext, name: str, on_step: Callable[[str], None] = _no_step) -> str | None:
    """Second step of delete: rename ``name``'s directory into ``.trash/``, then drop its secrets
    bindings and run directory, then remove the trashed tree.

    Bindings are dropped only once the directory move has succeeded: the
    caller restores the ledger entry as ``failed`` if this raises, and a
    restored (still-installed) app must keep its bindings, not lose them to a
    move it never actually completed. Safe to call again if interrupted
    midway -- a missing app directory is a no-op, not an error.

    Returns the trashed path (as a string) if it could not be removed even
    after escalating to :func:`purge_path`'s privileged fallback, or
    ``None`` once confirmed gone -- the caller surfaces a leftover on the
    job's ``error`` rather than silently swallowing it.
    """
    dest = ctx.app_dir(name)
    trash: Path | None = None
    if dest.exists():
        trash = ctx.trash_dir / ctx.new_id()
        on_step("trash")
        swap_lock = _ensure_not_running_before_swap(ctx, name)
        try:
            ctx.trash_dir.mkdir(parents=True, exist_ok=True)
            dest.rename(trash)
        finally:
            swap_lock.__exit__(None, None, None)
    ctx.secrets.delete_bindings(name)
    if ctx.run_dir is not None:
        ctx.run_dir.remove(name)
    try:
        if trash is None:
            return None
        on_step("cleanup")
        return purge_path(ctx, trash)
    finally:
        _purge_uv_cache(ctx, name)


def _purge_uv_cache(ctx: AppsJobContext, name: str) -> str | None:
    """Escalate a deleted app's uv cache through the same trash-then-purge contract as ``purge_path``.

    Cache contents are written by the sync unit's ``palmimo-app`` uid
    (design doc 3.10); a bare ``rmtree`` from Portal's own uid can EACCES,
    the same reason ``purge_path`` refuses anything not already staged in
    ``.trash/``.
    """
    cache = ctx.uv_cache_dir / name
    if not cache.exists() and not cache.is_symlink():
        return None
    trash = ctx.trash_dir / ctx.new_id()
    ctx.trash_dir.mkdir(parents=True, exist_ok=True)
    try:
        cache.rename(trash)
    except OSError as error:
        logger.warning("apps: could not move uv cache into .trash name=%s: %s", name, error)
        return str(cache)
    leftover = purge_path(ctx, trash)
    if leftover is not None:
        logger.warning("apps: purge left a leftover uv cache name=%s", name)
    return leftover


def check_git_update(ctx: AppsJobContext, record: AppRecord) -> tuple[bool, str | None]:
    """Compare public catalog commits or check a branch with live git; other tags stay pinned."""
    if is_public_source(record):
        status = public_app_status(ctx.public_catalog_cache, record)
        return status.update_available is True, status.latest_commit
    if record.source.type != "git":
        return False, None
    if record.source.ref_kind != "branch":
        return False, None
    assert record.source.url is not None and record.source.ref is not None
    remote_commit = ctx.git.fetch_commit(
        record.source.url, record.source.ref, record.source.ref_kind, env=git_env(ctx, record.source.url)
    )
    return remote_commit != record.source.commit, remote_commit


def _project_dir_for(ctx: AppsJobContext, record: AppRecord) -> Path:
    return resolve_subdir(ctx.app_dir(record.id), record.source.subdir)


def disk_state_checks(ctx: AppsJobContext, state: AppsState) -> tuple[Callable[[str], bool], Callable[[str], bool]]:
    """Build the ``manifest_exists``/``venv_exists`` callables :func:`~palmimo_portal.core.apps.finalize_apps_state` needs."""

    def manifest_exists(name: str) -> bool:
        record = state.apps.get(name)
        if record is None:
            return False
        return (_project_dir_for(ctx, record) / _manifest_filename_for(record.source)).is_file()

    def venv_exists(name: str) -> bool:
        record = state.apps.get(name)
        if record is None:
            return False
        try:
            layout = resolve_layout(ctx.app_dir(record.id), record.source.subdir)
        except InvalidManifestSourceError:
            return False
        return layout.venv_python.exists()

    return manifest_exists, venv_exists


def cleanup_staging_and_trash(ctx: AppsJobContext) -> None:
    """Remove every leftover ``.staging/*``/``.trash/*`` entry -- called at startup finalize.

    A leftover that even :func:`purge_path`'s privileged escalation cannot
    remove is logged at WARNING and left in place -- there is no job or
    response to attach it to at startup, so the log is the only signal.
    """
    for base in (ctx.staging_dir, ctx.trash_dir):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            purge_path(ctx, child)


def sweep_orphan_app_dirs(ctx: AppsJobContext, state: AppsState) -> None:
    """Trash any directory directly under ``apps_dir`` that the ledger does not name -- startup finalize.

    ``commit_install`` renames a staged tree to ``apps/<id>`` before its ledger
    record is written, and delete drops the ledger record before moving the
    directory to ``.trash`` -- a crash or restart in either window leaves a
    directory here that :func:`~palmimo_portal.core.apps.finalize_apps_state`
    (ledger-driven) never looks at, permanently blocking a reinstall of the same
    name. Safe to call every startup: a directory already in the ledger is left
    alone.
    """
    if not ctx.apps_dir.is_dir():
        return
    for child in ctx.apps_dir.iterdir():
        if child.name in (".staging", ".trash") or child.name in state.apps:
            continue
        logger.warning("apps: removing orphan app directory name=%s", child.name)
        ctx.trash_dir.mkdir(parents=True, exist_ok=True)
        trash = ctx.trash_dir / ctx.new_id()
        child.rename(trash)
        purge_path(ctx, trash)
