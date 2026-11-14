import contextlib
import io
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.api.app import create_app
from palmimo_portal.core.apps_job_runner import AppsJobRunner
from palmimo_portal.core.apps_jobs import AppsJobContext, commit_install, prepare_install_zip
from palmimo_portal.ports import (
    AppAssetsStatus,
    DisplayAssetConversion,
    DisplayAssetMessage,
    DisplayAssetsStatus,
    DisplayBusyError,
    DisplayToolError,
    RunLockTimeoutError,
    StandardAssetStatus,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


APP_ID = "zip.face"


def archive(names: tuple[str, ...] = ("welcome",), *, device: bool = True, slot: bool = False) -> bytes:
    text = 'schema=2\nname="face"\ndescription="Face"\ncommand=["run"]\n'
    if device:
        text += 'devices=["motor_display"]\n'
    for name in names:
        text += f'[assets.{name}]\nfile="{name}.json"\ndescription="{name}"\n'
        if slot:
            text += f'[assets.{name}.params.eye]\ndescription="Eye"\n'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        target.writestr("palmimo.toml", text)
        target.writestr("pyproject.toml", '[project]\nname="face"\nversion="0"\n')
        for name in names:
            target.writestr(f"{name}.json", "{}")
    return buffer.getvalue()


@pytest.fixture
def env(tmp_path: Path) -> tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner]:
    app = create_app(
        Settings(
            apps_dir=tmp_path / "apps",
            uv_cache_dir=tmp_path / "cache",
            apps_run_in_thread=False,
            periodic_enabled=False,
        )
    )
    adapters: FakeAdapterBundle = app.state.adapters
    adapters.app_environment.display_firmware_need = "2"
    return app.state.apps_job_context, adapters, app.state.apps_job_runner


@pytest.mark.parametrize("failure", ["conversion", "firmware", "device", "slot", "kind", "replace", "tool-capacity"])
def test_asset_install_failure_keeps_ledger_and_tree_unchanged(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], failure: str
) -> None:
    ctx, adapters, runner = env
    if failure == "conversion":
        adapters.asset_converter.result = replace(
            adapters.asset_converter.result,
            ok=False,
            errors=(DisplayAssetMessage("bad", "Invalid layer", "layers[0]"),),
        )
    elif failure == "firmware":
        adapters.app_environment.display_firmware_need = "1"
    elif failure == "slot":
        adapters.asset_converter.result = replace(adapters.asset_converter.result, params={})
    elif failure == "kind":
        adapters.asset_converter.result = replace(adapters.asset_converter.result, kind="image", params={"eye": 4})
    elif failure in {"replace", "tool-capacity"}:

        def busy(app_id: str, directory: Path) -> None:
            if failure == "tool-capacity":
                raise DisplayToolError("capacity", short_by=7)
            raise DisplayBusyError()

        adapters.platform.replace_display_assets = busy  # type: ignore[method-assign]
    prepared = prepare_install_zip(ctx, archive(device=failure != "device", slot=failure in {"slot", "kind"}))
    job = runner.start_install(prepared)
    assert job.state == "failed"
    assert (
        job.error_code
        == {
            "conversion": "display_asset_invalid",
            "firmware": "display_assets_require_v2",
            "device": "display_assets_require_v2",
            "slot": "manifest_invalid",
            "kind": "manifest_invalid",
            "replace": "display_busy",
            "tool-capacity": "display_capacity_exceeded",
        }[failure]
    )
    assert APP_ID not in adapters.state.read_apps_state().apps
    assert not ctx.app_dir(APP_ID).exists()
    assert not adapters.platform.replace_display_assets_calls
    if failure == "conversion":
        assert job.error is not None and "welcome" in job.error and "layers[0]" in job.error


@pytest.mark.parametrize(("capacity", "outcome"), [(20480, "done"), (20479, "failed")])
def test_asset_capacity_reserves_rounded_space_for_largest_asset(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], capacity: int, outcome: str
) -> None:
    ctx, adapters, runner = env
    adapters.asset_converter.result = replace(adapters.asset_converter.result, bytes=4097)
    adapters.platform.display_assets = DisplayAssetsStatus(
        capacity,
        (StandardAssetStatus("happy", 1, "vector", (), True, 999999, "image", ()),),
        {"other.app": AppAssetsStatus(999999, 999999, ("old",))},
        (),
    )
    job = runner.start_install(prepare_install_zip(ctx, archive()))
    assert job.state == outcome
    if outcome == "failed":
        assert job.error_code == "display_capacity_exceeded"
        assert job.error is not None and "1" in job.error
        assert not adapters.platform.replace_display_assets_calls
    else:
        assert adapters.state.read_apps_state().apps[APP_ID].display_firmware == "2"
        assert not (ctx.app_dir(APP_ID) / ".display-assets").exists()


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("new_names", [("a", "c"), ()])
def test_asset_update_replaces_whole_set_before_swap(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], tmp_path: Path, new_names: tuple[str, ...], busy: bool
) -> None:
    ctx, adapters, runner = env
    assert runner.start_install(prepare_install_zip(ctx, archive(("a", "b")))).state == "done"
    record = adapters.state.read_apps_state().apps[APP_ID]
    source = replace(record.source, type="git", url="https://example.com/repo", ref="main", ref_kind="branch")
    adapters.state.update_apps_state(lambda state: replace(state, apps={APP_ID: replace(record, source=source)}))

    def clone(url: str, ref: str, ref_kind: str, dest: Path, **kwargs: Any) -> str:
        dest.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(archive(new_names))) as zipped:
            zipped.extractall(dest)
        return "new-commit"

    adapters.git.clone_shallow = clone  # type: ignore[method-assign]
    seen: list[set[str]] = []

    def replace_assets(app_id: str, directory: Path) -> None:
        assert app_id == APP_ID
        seen.append({path.stem for path in directory.glob("*.bin")})
        assert (ctx.app_dir(APP_ID) / "b.json").exists()
        if busy:
            raise DisplayBusyError()

    adapters.platform.replace_display_assets = replace_assets  # type: ignore[method-assign]
    job = runner.start_update(APP_ID)
    assert job.state == ("failed" if busy else "done")
    assert seen == [set(new_names)]
    assert not adapters.platform.uninstall_display_assets_calls
    updated = adapters.state.read_apps_state().apps[APP_ID]
    assert updated.manifest is not None
    assert set(updated.manifest["assets"]) == ({"a", "b"} if busy else set(new_names))
    if busy:
        assert job.error_code == "display_busy"
        assert (ctx.app_dir(APP_ID) / "b.json").exists()
        assert not (ctx.app_dir(APP_ID) / "c.json").exists()
    assert updated.display_firmware == "2"


def test_asset_stage_only_locks_status_and_replace(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner],
) -> None:
    ctx, adapters, _ = env
    events: list[str] = []
    convert = adapters.asset_converter.convert

    def convert_asset(src: Path, dst: Path) -> DisplayAssetConversion:
        with adapters.state.lock_run():
            events.append("convert")
        return convert(src, dst)

    adapters.asset_converter.convert = convert_asset  # type: ignore[method-assign]

    def status() -> DisplayAssetsStatus:
        with pytest.raises(RunLockTimeoutError), adapters.state.lock_run():
            pass
        events.append("status")
        return adapters.platform.display_assets

    adapters.platform.display_assets_status = status  # type: ignore[method-assign]

    def replace_assets(app_id: str, directory: Path) -> None:
        with pytest.raises(RunLockTimeoutError), adapters.state.lock_run():
            pass
        events.append("replace")

    adapters.platform.replace_display_assets = replace_assets  # type: ignore[method-assign]

    def step(name: str) -> None:
        if name == "swap":
            with adapters.state.lock_run():
                events.append("swap")

    prepared = prepare_install_zip(ctx, archive())
    commit_install(ctx, adapters.state.read_apps_state(), prepared, 0, on_step=step)
    assert events == ["convert", "status", "replace", "swap"]


@pytest.mark.parametrize("release", [False, True])
def test_asset_stage_waits_for_run_lock_or_fails_without_swap(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], monkeypatch: pytest.MonkeyPatch, release: bool
) -> None:
    ctx, adapters, runner = env
    attempted = threading.Event()
    original = adapters.state.lock_run

    @contextlib.contextmanager
    def lock_run(timeout_s: float = 0) -> Iterator[None]:
        if timeout_s:
            assert timeout_s == 300
            attempted.set()
        with original(timeout_s=1 if release else 0.01):
            yield

    monkeypatch.setattr(adapters.state, "lock_run", lock_run)
    prepared = prepare_install_zip(ctx, archive())
    results = []
    with original():
        thread = threading.Thread(target=lambda: results.append(runner.start_install(prepared)))
        thread.start()
        assert attempted.wait(2)
        assert not adapters.platform.replace_display_assets_calls
        if not release:
            thread.join(2)
    thread.join(2)
    assert not thread.is_alive()
    assert results[0].state == ("done" if release else "failed")
    if not release:
        assert results[0].error_code == "display_busy"
        assert not ctx.app_dir(APP_ID).exists()


def test_asset_job_error_survives_state_reload(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], tmp_path: Path
) -> None:
    from palmimo_portal.adapters.state import JsonFileStateStore

    ctx, adapters, _ = env
    store = JsonFileStateStore(tmp_path / "state")
    ctx = replace(ctx, state=store)
    runner = AppsJobRunner(store, ctx, adapters.app_unit, run_in_thread=False)
    adapters.asset_converter.result = replace(adapters.asset_converter.result, ok=False)
    assert runner.start_install(prepare_install_zip(ctx, archive())).state == "failed"
    job = JsonFileStateStore(tmp_path / "state").read_apps_state().last_orphan_job
    assert job is not None and job.error_code == "display_asset_invalid"


@pytest.mark.parametrize("failure", [None, "busy", "lock"])
def test_asset_delete_uninstalls_after_unregister_and_tolerates_failure(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    ctx, adapters, runner = env
    assert runner.start_install(prepare_install_zip(ctx, archive())).state == "done"
    calls = []

    def uninstall(app_id: str) -> None:
        assert app_id not in adapters.state.read_apps_state().apps
        with pytest.raises(RunLockTimeoutError), adapters.state.lock_run():
            pass
        calls.append(app_id)
        if failure == "busy":
            raise DisplayBusyError()

    monkeypatch.setattr(adapters.platform, "uninstall_display_assets", uninstall)
    original = adapters.state.lock_run

    @contextlib.contextmanager
    def lock_run(timeout_s: float = 0) -> Iterator[None]:
        if timeout_s == 300:
            raise RunLockTimeoutError()
        with original(timeout_s=timeout_s):
            yield

    if failure == "lock":
        monkeypatch.setattr(adapters.state, "lock_run", lock_run)
    assert runner.start_delete(APP_ID).state == "done"
    assert calls == ([] if failure == "lock" else [APP_ID])
    assert APP_ID not in adapters.state.read_apps_state().apps
    assert not ctx.app_dir(APP_ID).exists()


def test_asset_stage_is_skipped_without_old_or_new_assets(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner],
) -> None:
    ctx, adapters, runner = env
    assert runner.start_install(prepare_install_zip(ctx, archive(()))).state == "done"
    assert adapters.platform.display_assets_status_calls == 0
    assert not adapters.platform.replace_display_assets_calls
    assert adapters.state.read_apps_state().apps[APP_ID].display_firmware == "2"


def test_asset_install_preserves_declared_slots_and_converted_content(
    env: tuple[AppsJobContext, FakeAdapterBundle, AppsJobRunner], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, adapters, runner = env
    adapters.asset_converter.result = replace(adapters.asset_converter.result, params={"eye": 4})
    adapters.asset_converter.content = b"converted asset"
    copied = []

    def replace_assets(app_id: str, directory: Path) -> None:
        copied.append((app_id, (directory / "welcome.bin").read_bytes()))

    monkeypatch.setattr(adapters.platform, "replace_display_assets", replace_assets)
    assert runner.start_install(prepare_install_zip(ctx, archive(slot=True))).state == "done"
    record = adapters.state.read_apps_state().apps[APP_ID]
    assert record.manifest is not None
    assert record.manifest["assets"]["welcome"]["params"] == {"eye": {"description": "Eye"}}
    assert copied == [(APP_ID, b"converted asset")]
    assert adapters.platform.display_firmware_calls == 0
    assert not adapters.platform.sync_display_assets_calls
