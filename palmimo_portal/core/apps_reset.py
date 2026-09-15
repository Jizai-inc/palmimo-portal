"""``POST /apps/reset``: the SSH-free way back to an empty app platform (design doc 3.2).

Stops every active ``palmimo-app@*`` unit first, then erases the app tree,
the secret store, and the ledger -- in that order, so a reset can never
delete a running app's files out from under it. ``api/apps.py`` owns the
mutual-exclusion checks (``apps.lock``/``run.lock``, a Portal or platform
update in progress); this module assumes it already holds both locks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from palmimo_portal.core.apps_jobs import AppsJobContext, purge_path
from palmimo_portal.core.platform_update import IDLE_PLATFORM_STATE
from palmimo_portal.ports import AppsState, AppUnitPort, RunDirPort, SecretsStore, StateStore


logger = logging.getLogger("palmimo_portal")


def _clear_staging_or_trash(ctx: AppsJobContext, base: Path) -> list[str]:
    """Purge every direct child of ``.staging/`` or ``.trash/`` -- already the shape :func:`~palmimo_portal.core.apps_jobs.purge_path` accepts."""
    if not base.is_dir():
        return []
    return [str(child) for child in base.iterdir() if purge_path(ctx, child) is not None]


def _clear_directory_contents(ctx: AppsJobContext, path: Path) -> list[str]:
    """Delete everything under *path*, leaving the directory itself (and its ownership/setgid bit) intact.

    ``apps_dir``/``uv_cache_dir`` are provisioned by the platform bundle
    with a specific owner and the setgid bit set (design doc 2.7) --
    recreating the directory after an ``rmtree`` would lose both until the
    next platform-bundle apply. Each child (other than ``.staging``/``.trash``
    themselves, cleared separately by :func:`_clear_staging_or_trash`) is
    renamed into ``.trash/<uuid>/`` before
    :func:`~palmimo_portal.core.apps_jobs.purge_path` runs on it -- that call
    accepts only a direct ``.staging/``/``.trash/`` child, mirroring the
    on-device ``app-sync`` helper's own purge-mode contract. Returns every
    child that could not be removed even after ``purge_path``'s privileged
    escalation, rather than raising.
    """
    if not path.is_dir():
        return []
    leftover: list[str] = []
    for child in path.iterdir():
        if child in (ctx.staging_dir, ctx.trash_dir):
            continue
        trash = ctx.trash_dir / ctx.new_id()
        ctx.trash_dir.mkdir(parents=True, exist_ok=True)
        try:
            child.rename(trash)
        except OSError as error:
            logger.warning("apps: reset could not move %s into .trash: %s", child, error)
            leftover.append(str(child))
            continue
        if purge_path(ctx, trash) is not None:
            leftover.append(str(child))
    return leftover


@dataclass(frozen=True)
class ResetResult:
    """What :func:`reset_platform` removed, and any leftover it could not."""

    deleted: list[str]
    leftover_paths: list[str]


def reset_platform(
    state: StateStore, ctx: AppsJobContext, app_unit: AppUnitPort, run_dir: RunDirPort, secrets: SecretsStore
) -> ResetResult:
    """Stop every active app, then erase the contents of ``apps/``/``uv-cache/``, every run dir, secrets, and the ledger.

    ``catalog_cache.json`` is deliberately untouched -- design doc 3.2:
    reset returns the app platform to its initial state, not to "never
    contacted GitHub". The caller (``api/apps.py``) already guarantees no
    platform-bundle update job is running before calling this, so
    ``platform_update.json`` is unconditionally reset to idle here too.
    A path :func:`_clear_directory_contents` could not remove is reported
    in ``ResetResult.leftover_paths`` rather than silently dropped or
    failing the whole reset -- every other step still runs.
    """
    for name in sorted(app_unit.list_active_app_units()):
        app_unit.stop(name)

    deleted = [str(ctx.apps_dir), str(ctx.uv_cache_dir)]
    leftover_paths: list[str] = []
    leftover_paths.extend(_clear_staging_or_trash(ctx, ctx.staging_dir))
    leftover_paths.extend(_clear_staging_or_trash(ctx, ctx.trash_dir))
    for tree in (ctx.apps_dir, ctx.uv_cache_dir):
        leftover_paths.extend(_clear_directory_contents(ctx, tree))

    run_dir.remove_all()
    deleted.append("run directories")

    secrets.reset()
    deleted.append("secrets")

    state.write_apps_state(AppsState())
    deleted.append("apps.json")

    state.write_platform_update_state(IDLE_PLATFORM_STATE)
    deleted.append("platform_update.json")

    if leftover_paths:
        logger.warning("apps: platform reset left paths behind leftover_paths=%s", leftover_paths)
    logger.info("apps: platform reset by operator paths=%s", deleted)
    return ResetResult(deleted=deleted, leftover_paths=leftover_paths)
