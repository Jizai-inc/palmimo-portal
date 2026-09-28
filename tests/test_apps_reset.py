"""Behavioral tests for `core/apps_reset.py`: `POST /apps/reset`'s core logic."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from palmimo_portal.core.apps_jobs import AppsJobContext
from palmimo_portal.core.apps_reset import ResetStopError, reset_platform
from palmimo_portal.ports import AppRecord, AppSource, AppsState, UnitStatus
from palmimo_portal.testing.fakes import (
    FakeAppUnitPort,
    FakeDiskPort,
    FakeGitPort,
    FakeJournalPort,
    FakeRunDirPort,
    FakeSecretsStore,
    FakeStateStore,
    FakeSyncUnitPort,
    FakeUvPort,
)


@pytest.fixture
def ctx(tmp_path: Path) -> AppsJobContext:
    apps_dir = tmp_path / "apps"
    (apps_dir / "some-app").mkdir(parents=True)
    # The platform bundle provisions `.staging`/`.trash` as siblings of every installed app
    # (design doc 2.2b) -- present on every real device, so a fixture without them would miss
    # the rename-into-.trash-then-purge path reset must take for `some-app` itself.
    (apps_dir / ".staging").mkdir()
    (apps_dir / ".trash").mkdir()
    uv_cache_dir = tmp_path / "uv-cache"
    uv_cache_dir.mkdir()
    return AppsJobContext(
        apps_dir=apps_dir,
        uv_cache_dir=uv_cache_dir,
        git=FakeGitPort(),
        uv=FakeUvPort(),
        sync_unit=FakeSyncUnitPort(),
        journal=FakeJournalPort(),
        disk=FakeDiskPort(),
        secrets=FakeSecretsStore(),
    )


def test_reset_platform_stops_active_units_before_deleting_anything(ctx: AppsJobContext) -> None:
    state = FakeStateStore()
    app_unit = FakeAppUnitPort()
    app_unit.simulate_active_state(
        "running-app", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )
    run_dir = FakeRunDirPort()
    secrets = FakeSecretsStore()
    order: list[str] = []
    original_stop = app_unit.stop
    original_remove_all = run_dir.remove_all

    def tracked_stop(name: str) -> None:
        order.append(f"stop:{name}")
        original_stop(name)

    def tracked_remove_all() -> None:
        order.append("remove_all")
        original_remove_all()

    app_unit.stop = tracked_stop  # type: ignore[method-assign]
    run_dir.remove_all = tracked_remove_all  # type: ignore[method-assign]

    reset_platform(state, ctx, app_unit, run_dir, secrets)

    assert order == ["stop:running-app", "remove_all"]


def test_reset_platform_waits_for_inactive_before_deleting_anything(ctx: AppsJobContext) -> None:
    state = FakeStateStore()
    app_unit = FakeAppUnitPort()
    app_unit.simulate_active_state(
        "running-app", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )
    original_stop = app_unit.stop
    statuses = iter(
        (
            UnitStatus(active_state="deactivating", sub_state="stop", result="success", exec_main_status=0),
            UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0),
        )
    )
    became_inactive_before_remove = False

    def delayed_status(name: str) -> UnitStatus:
        nonlocal became_inactive_before_remove
        status = next(statuses)
        became_inactive_before_remove = status.active_state == "inactive"
        return status

    app_unit.stop = lambda name: original_stop(name)  # type: ignore[method-assign]
    app_unit.status = delayed_status  # type: ignore[method-assign]
    run_dir = FakeRunDirPort()
    original_remove_all = run_dir.remove_all

    def assert_stopped_before_remove() -> None:
        assert became_inactive_before_remove
        original_remove_all()

    run_dir.remove_all = assert_stopped_before_remove  # type: ignore[method-assign]

    reset_platform(state, ctx, app_unit, run_dir, FakeSecretsStore())


def test_reset_platform_leaves_data_untouched_when_a_unit_does_not_stop(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    app_unit = FakeAppUnitPort()
    app_unit.simulate_active_state(
        "running-app", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )
    app_unit.stop = lambda name: None  # type: ignore[method-assign]
    monkeypatch.setattr("palmimo_portal.core.apps_reset.STOP_WAIT_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(ResetStopError):
        reset_platform(FakeStateStore(), ctx, app_unit, FakeRunDirPort(), FakeSecretsStore())

    assert (ctx.apps_dir / "some-app").exists()


def test_reset_platform_succeeds_when_a_unit_lands_in_failed_after_stop(ctx: AppsJobContext) -> None:
    # A unit SIGKILL'd by `stop` (e.g. `TimeoutStopSec` expiring) reports `failed`, not
    # `inactive` -- reset must treat that as "no longer running" rather than waiting forever.
    app_unit = FakeAppUnitPort()
    app_unit.simulate_active_state(
        "running-app", UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )
    original_stop = app_unit.stop

    def stop_and_fail(name: str) -> None:
        original_stop(name)
        app_unit.simulate_active_state(
            name, UnitStatus(active_state="failed", sub_state="failed", result="timeout", exec_main_status=1)
        )

    app_unit.stop = stop_and_fail  # type: ignore[method-assign]

    reset_platform(FakeStateStore(), ctx, app_unit, FakeRunDirPort(), FakeSecretsStore())


def test_reset_platform_empties_apps_and_uv_cache_directories_without_removing_them(ctx: AppsJobContext) -> None:
    # apps_dir/uv_cache_dir are provisioned by the platform bundle with a specific owner and the
    # setgid bit set; deleting and never recreating them would lose both until the next apply.
    reset_platform(FakeStateStore(), ctx, FakeAppUnitPort(), FakeRunDirPort(), FakeSecretsStore())

    assert ctx.apps_dir.is_dir()
    assert set(ctx.apps_dir.iterdir()) == {ctx.staging_dir, ctx.trash_dir}
    assert list(ctx.staging_dir.iterdir()) == []
    assert list(ctx.trash_dir.iterdir()) == []
    assert ctx.uv_cache_dir.is_dir()
    assert list(ctx.uv_cache_dir.iterdir()) == []


def test_reset_platform_moves_an_undeletable_app_dir_into_trash_and_purges_it_there(
    monkeypatch: pytest.MonkeyPatch, ctx: AppsJobContext
) -> None:
    # purge_path only ever accepts a direct .staging/.trash child (mirrors the on-device
    # app-sync helper's own purge-mode contract) -- an app directory living straight under
    # apps_dir must be renamed into .trash/<uuid>/ first, never purged in place.
    from palmimo_portal.core import apps_jobs

    trash_container = ctx.trash_dir / "stuck-trash"
    real_rmtree = shutil.rmtree

    def fail_only_the_trash_container(path: str | Path, ignore_errors: bool = False, **_: object) -> None:
        if Path(path) == trash_container:
            raise OSError("busy")
        real_rmtree(path, ignore_errors=ignore_errors)

    ctx.new_id = lambda: "stuck-trash"
    monkeypatch.setattr(apps_jobs.shutil, "rmtree", fail_only_the_trash_container)
    sync_unit = ctx.sync_unit
    assert isinstance(sync_unit, FakeSyncUnitPort)
    sync_unit.staging_dir = ctx.staging_dir
    sync_unit.undeletable_paths.add(str(trash_container))

    result = reset_platform(FakeStateStore(), ctx, FakeAppUnitPort(), FakeRunDirPort(), FakeSecretsStore())

    assert result.leftover_paths == [str(ctx.apps_dir / "some-app")]
    assert not (ctx.apps_dir / "some-app").exists()
    purge_instance = sync_unit.start_calls[-1]
    assert sync_unit.sync_specs[purge_instance] == {"purge": str(trash_container)}
    assert ctx.staging_dir.is_dir()
    assert ctx.trash_dir.is_dir()


def test_reset_platform_resets_the_platform_update_job_to_idle(ctx: AppsJobContext) -> None:
    from palmimo_portal.ports import PlatformJob, PlatformUpdateState

    state = FakeStateStore()
    state.write_platform_update_state(
        PlatformUpdateState(
            job=PlatformJob(
                state="failed", target_version=2, step="verify", error="boom", started_at=1.0, finished_at=2.0
            )
        )
    )

    reset_platform(state, ctx, FakeAppUnitPort(), FakeRunDirPort(), FakeSecretsStore())

    assert state.read_platform_update_state().job.state == "idle"


def test_reset_platform_clears_secrets_and_bindings(ctx: AppsJobContext) -> None:
    secrets = FakeSecretsStore()
    secrets.set_secret("MY_KEY", "value")
    secrets.write_bindings("app", {"API_KEY": "MY_KEY"})
    secrets.set_git_credential("github.com/x", "token")

    reset_platform(FakeStateStore(), ctx, FakeAppUnitPort(), FakeRunDirPort(), secrets)

    assert secrets.list_secrets() == []
    assert secrets.read_bindings("app") == {}
    assert secrets.list_git_credentials() == []


def test_reset_platform_empties_the_app_ledger(ctx: AppsJobContext) -> None:
    state = FakeStateStore()
    record = AppRecord(
        name="app", source=AppSource(type="zip"), installed_at=0.0, params={}, autostart=False, last_job=None
    )
    state.write_apps_state(AppsState(apps={"app": record}))

    reset_platform(state, ctx, FakeAppUnitPort(), FakeRunDirPort(), FakeSecretsStore())

    assert state.read_apps_state().apps == {}


def test_reset_platform_leaves_the_catalog_cache_untouched(ctx: AppsJobContext) -> None:
    from palmimo_portal.ports import CatalogCacheState

    state = FakeStateStore()
    cache = CatalogCacheState(tag="v1.0.0", apps=(), fetched_at=1.0)
    state.write_catalog_cache(cache)

    reset_platform(state, ctx, FakeAppUnitPort(), FakeRunDirPort(), FakeSecretsStore())

    assert state.read_catalog_cache() == cache
