"""Behavioral tests for the install/update/delete job pipelines (core/apps_jobs.py)."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from palmimo_portal.core.apps_jobs import (
    AppsJobContext,
    check_git_update,
    commit_install,
    delete_from_ledger,
    install_git,
    install_zip,
    prepare_install_git,
    prepare_install_zip,
    preview_git,
    purge_app_files,
    sweep_orphan_app_dirs,
    sync_unit_name,
    update_git,
)
from palmimo_portal.core.catalog import CatalogCache
from palmimo_portal.core.manifest import ManifestValidationError
from palmimo_portal.ports import (
    AppNotFoundError,
    AppRecord,
    AppSource,
    AppsState,
    CatalogAsset,
    DiskFullError,
    GitCommandError,
    InvalidManifestSourceError,
    JournalEntry,
    SyncFailedError,
    UnitStatus,
)
from palmimo_portal.testing.fakes import (
    FakeAppUnitPort,
    FakeCatalogSource,
    FakeDiskPort,
    FakeGitPort,
    FakeJournalPort,
    FakeSecretsStore,
    FakeStateStore,
    FakeSyncUnitPort,
    FakeUvPort,
    make_catalog_app,
)


ZIP_ID = "zip.palmimo-teleop"
GIT_ID = "git.palmimo-teleop"


def _zip_bytes(name: str = "app", *, with_lock: bool = False, top_dir: str | None = None) -> bytes:
    prefix = f"{top_dir}/" if top_dir else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{prefix}palmimo.toml", f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n'
        )
        archive.writestr(f"{prefix}pyproject.toml", "[project]\nname='app'\nversion='0'\n")
        if with_lock:
            archive.writestr(f"{prefix}uv.lock", "")
    return buffer.getvalue()


@dataclass
class Harness:
    """Bundles a real :class:`AppsJobContext` with its concrete fakes, typed narrowly.

    Mirrors :class:`~palmimo_portal.testing.fakes.FakeAdapterBundle`'s own
    reason for existing: ``AppsJobContext``'s fields are typed to the port
    *protocols*, correct for the production code but too narrow for a test
    that pokes fake-only attributes (``git.next_commit``, ``disk.free_bytes_value``).
    """

    ctx: AppsJobContext
    git: FakeGitPort
    uv: FakeUvPort
    sync_unit: FakeSyncUnitPort
    journal: FakeJournalPort
    disk: FakeDiskPort
    secrets: FakeSecretsStore


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    git, uv, disk, secrets = FakeGitPort(), FakeUvPort(), FakeDiskPort(), FakeSecretsStore()
    sync_unit, journal = FakeSyncUnitPort(), FakeJournalPort()
    ctx = AppsJobContext(
        apps_dir=tmp_path / "apps",
        uv_cache_dir=tmp_path / "uv-cache",
        git=git,
        uv=uv,
        sync_unit=sync_unit,
        journal=journal,
        disk=disk,
        secrets=secrets,
    )
    return Harness(ctx=ctx, git=git, uv=uv, sync_unit=sync_unit, journal=journal, disk=disk, secrets=secrets)


def test_install_zip_registers_app_and_moves_it_into_apps_dir(harness: Harness) -> None:
    state, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert record.name == "palmimo-teleop"
    assert state.apps[ZIP_ID] is record
    assert (harness.ctx.apps_dir / ZIP_ID / "palmimo.toml").is_file()


def test_install_zip_removes_staging_directory_on_success(harness: Harness) -> None:
    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert list(harness.ctx.staging_dir.glob("*")) == []


def test_install_zip_removes_staging_directory_on_failure(harness: Harness) -> None:
    with pytest.raises(InvalidManifestSourceError):
        install_zip(harness.ctx, AppsState(), b"not a zip")

    assert list(harness.ctx.staging_dir.glob("*")) == []


def test_install_zip_chmods_the_fetched_tree_before_sync_unit_starts(harness: Harness) -> None:
    modes: list[int] = []
    original_start = harness.sync_unit.start

    def tracking_start(instance: str) -> None:
        root = harness.ctx.staging_dir / instance
        modes.append(stat.S_IMODE(root.stat().st_mode))
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]

    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert modes[0] & (stat.S_IWGRP | stat.S_ISGID) == (stat.S_IWGRP | stat.S_ISGID)


def test_install_zip_does_not_chmod_anything_after_the_sync_unit_returns(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sync runs as palmimo-app -- a chmod after wait() returns would EPERM on any file/dir
    # the unit itself created.
    wait_returned = False
    original_wait = harness.sync_unit.wait

    def tracking_wait(instance: str, timeout_s: float) -> UnitStatus:
        nonlocal wait_returned
        result = original_wait(instance, timeout_s)
        wait_returned = True
        return result

    harness.sync_unit.wait = tracking_wait  # type: ignore[method-assign]

    chmod_calls_after_wait: list[Path] = []
    original_chmod = Path.chmod

    def tracking_chmod(self: Path, *args: object, **kwargs: object) -> None:
        if wait_returned:
            chmod_calls_after_wait.append(self)
        original_chmod(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "chmod", tracking_chmod)

    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert chmod_calls_after_wait == []


def test_install_zip_peeled_top_dir_is_2775_before_sync(harness: Harness) -> None:
    modes: list[int] = []
    original_start = harness.sync_unit.start

    def tracking_start(instance: str) -> None:
        root = harness.ctx.staging_dir / instance / "palmimo-teleop-1.0"
        modes.append(stat.S_IMODE(root.stat().st_mode))
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]

    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop", top_dir="palmimo-teleop-1.0"))

    assert modes[0] == 0o2775


def test_install_zip_uses_a_suffix_for_a_second_same_source(harness: Harness) -> None:
    state, _ = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    next_state, record = install_zip(harness.ctx, state, _zip_bytes("palmimo-teleop"))

    assert record.id == "zip.palmimo-teleop-2"
    assert record.id in next_state.apps


def test_install_zip_below_disk_reserve_is_refused(harness: Harness) -> None:
    harness.disk.free_bytes_value = 0

    with pytest.raises(DiskFullError):
        install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))


def test_install_zip_generates_lock_when_uv_lock_is_missing(harness: Harness) -> None:
    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert record.last_job is not None
    assert record.last_job.lock_generated is True


def test_install_zip_skips_lock_generation_when_uv_lock_is_present(harness: Harness) -> None:
    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop", with_lock=True))

    assert record.last_job is not None
    assert record.last_job.lock_generated is False


def test_install_zip_starts_the_sync_unit_with_a_frozen_sync_request(harness: Harness) -> None:
    original_start = harness.sync_unit.start
    captured: dict[str, dict[str, object]] = {}

    def tracking_start(instance: str) -> None:
        spec_text = (harness.ctx.staging_dir / instance / "sync.json").read_text(encoding="utf-8")
        captured["spec"] = json.loads(spec_text)
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]

    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop", with_lock=True))

    assert record.last_job is not None
    assert captured["spec"]["frozen"] is True
    assert "package" not in captured["spec"]
    assert harness.sync_unit.start_calls == [record.last_job.id]


def test_install_zip_does_not_leave_sync_json_in_the_installed_app_tree(harness: Harness) -> None:
    # sync.json only ever needs to exist for the sync unit's own run -- the app tree it lands
    # in afterward must not keep carrying it around.
    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop", with_lock=True))

    assert not (harness.ctx.apps_dir / "palmimo-teleop" / "sync.json").exists()


def test_install_discards_a_cache_for_an_unregistered_id(harness: Harness) -> None:
    app_cache = harness.ctx.uv_cache_dir / ZIP_ID
    app_cache.mkdir(parents=True, exist_ok=True)
    (app_cache / "prior-cache").write_text("reusable")
    other_cache = harness.ctx.uv_cache_dir / "zip.other-app"
    other_cache.mkdir()
    (other_cache / "private-cache").write_text("other")
    seen: dict[str, set[str]] = {}
    original_start = harness.sync_unit.start

    def tracking_start(instance: str) -> None:
        staged_cache = harness.ctx.staging_dir / instance / ".uv-cache"
        seen["entries"] = {path.name for path in staged_cache.iterdir()}
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]
    install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    assert seen["entries"] == set()
    assert not (app_cache / "prior-cache").exists()
    assert (other_cache / "private-cache").is_file()
    assert not (harness.ctx.apps_dir / ZIP_ID / ".uv-cache").exists()


def test_sync_requests_interpreter_when_system_python_does_not_satisfy_requirement(harness: Harness) -> None:
    upload = io.BytesIO()
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("palmimo.toml", 'schema = 1\nname = "app"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\nrequires-python='>=3.13'\n")
    harness.uv.system_python_satisfies = False

    _, record = install_zip(harness.ctx, AppsState(), upload.getvalue())

    assert harness.uv.install_python_calls == [">=3.13"]
    assert harness.uv.system_python_calls == [">=3.13"]
    assert record.requires_python == ">=3.13"


def test_sync_skips_interpreter_install_when_system_python_satisfies_requirement(harness: Harness) -> None:
    upload = io.BytesIO()
    with zipfile.ZipFile(upload, "w") as archive:
        archive.writestr("palmimo.toml", 'schema = 1\nname = "app"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\nname='app'\nversion='0'\nrequires-python='>=3.12'\n")

    install_zip(harness.ctx, AppsState(), upload.getvalue())

    assert harness.uv.install_python_calls == []
    assert harness.uv.system_python_calls == [">=3.12"]


def test_install_zip_sync_failure_raises_with_masked_journal_tail(harness: Harness) -> None:
    harness.sync_unit.default_status = UnitStatus(
        active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1
    )
    prepared = prepare_install_zip(harness.ctx, _zip_bytes("palmimo-teleop"))
    harness.journal.entries_by_unit[sync_unit_name(prepared.job_id)] = [
        JournalEntry(message="GIT_CONFIG_VALUE_0=Authorization: Basic secret", timestamp=None, invocation_id=None),
        JournalEntry(message="error: could not read Username", timestamp=None, invocation_id=None),
    ]

    with pytest.raises(SyncFailedError) as excinfo:
        commit_install(harness.ctx, AppsState(), prepared, 0.0)

    assert "secret" not in str(excinfo.value)
    assert "could not read Username" in str(excinfo.value)


def test_install_zip_rejects_manifest_that_fails_validation(harness: Harness) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("palmimo.toml", 'schema = 1\nname = "Bad Name"\ndescription = "d"\ncommand = ["run"]\n')
        archive.writestr("pyproject.toml", "[project]\n")
    with pytest.raises(ManifestValidationError):
        install_zip(harness.ctx, AppsState(), buffer.getvalue())


def _seed_git_clone(dest: Path, name: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "palmimo.toml").write_text(f'schema = 1\nname = "{name}"\ndescription = "d"\ncommand = ["run"]\n')
    (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")


def test_install_git_records_commit_and_source(harness: Harness) -> None:
    harness.git.next_commit = "abc123"
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")

    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")

    assert record.source == AppSource(
        type="git", url="https://example.com/repo", ref="main", ref_kind="branch", commit="abc123"
    )
    assert state.apps[GIT_ID] is record


def test_install_git_rejects_subdir_that_escapes_the_clone_root(harness: Harness) -> None:
    harness.git.next_commit = "abc123"
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")

    with pytest.raises(InvalidManifestSourceError):
        install_git(
            harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch", subdir="../../etc"
        )


def test_install_git_workspace_member_is_frozen_when_uv_lock_is_at_the_workspace_root(harness: Harness) -> None:
    # uv resolves a workspace member against the lock at the workspace root, not one
    # inside the member's own directory -- `_sync_dependencies` must search up to the
    # clone root, or it would wrongly decide no lock exists and regenerate one.
    def seed_workspace(dest: Path, *_: object) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = ['member']\n")
        (dest / "uv.lock").write_text("")
        member = dest / "member"
        member.mkdir()
        (member / "palmimo.toml").write_text(
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run"]\n'
        )
        (member / "pyproject.toml").write_text("[project]\nname = 'actual-package-name'\n")

    harness.git.on_clone = seed_workspace
    captured: dict[str, dict[str, object]] = {}
    original_start = harness.sync_unit.start

    def tracking_start(instance: str) -> None:
        spec_text = (harness.ctx.staging_dir / instance / "sync.json").read_text(encoding="utf-8")
        captured["spec"] = json.loads(spec_text)
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]

    _, record = install_git(
        harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch", subdir="member"
    )

    assert captured["spec"]["frozen"] is True
    assert record.last_job is not None
    assert record.last_job.lock_generated is False


def _seed_git_clone_with_two_manifests(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "palmimo.toml").write_text('schema = 1\nname = "palmimo-default"\ndescription = "d"\ncommand = ["run"]\n')
    (dest / "palmimo.realtime.toml").write_text(
        'schema = 1\nname = "palmimo-realtime"\ndescription = "d"\ncommand = ["run"]\n'
    )
    (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")


def test_install_git_with_a_named_manifest_installs_the_app_it_declares(harness: Harness) -> None:
    # Without this, a directory shipping several manifests could only ever install whatever
    # app palmimo.toml declares, no matter which manifest file the caller asked for.
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone_with_two_manifests(dest)

    state, record = install_git(
        harness.ctx,
        AppsState(),
        url="https://example.com/repo",
        ref="main",
        ref_kind="branch",
        manifest_filename="palmimo.realtime.toml",
    )

    assert record.name == "palmimo-realtime"
    assert record.source.manifest == "palmimo.realtime.toml"
    assert "git.palmimo-default" not in state.apps


def test_update_git_re_reads_the_records_own_non_default_manifest(harness: Harness) -> None:
    # Without this, updating an app installed from a non-default manifest would silently fall
    # back to reading palmimo.toml, either missing entirely or belonging to a different app.
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone_with_two_manifests(dest)
    state, _ = install_git(
        harness.ctx,
        AppsState(),
        url="https://example.com/repo",
        ref="main",
        ref_kind="branch",
        manifest_filename="palmimo.realtime.toml",
    )

    harness.git.next_commit = "v2"
    _, new_record = update_git(harness.ctx, state, "git.palmimo-realtime")

    assert new_record.name == "palmimo-realtime"
    assert new_record.source.commit == "v2"
    assert new_record.source.manifest == "palmimo.realtime.toml"


def test_update_git_swaps_in_new_tree_and_updates_commit(harness: Harness) -> None:
    harness.git.next_commit = "v1"
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    marker = harness.ctx.apps_dir / record.id / "v1-marker.txt"
    marker.write_text("v1")

    harness.git.next_commit = "v2"

    def reseed(dest: Path, *_: object) -> None:
        _seed_git_clone(dest, "palmimo-teleop")
        (dest / "v2-marker.txt").write_text("v2")

    harness.git.on_clone = reseed
    _, new_record = update_git(harness.ctx, state, record.id)

    assert new_record.source.commit == "v2"
    assert (harness.ctx.apps_dir / record.id / "v2-marker.txt").is_file()
    assert not marker.exists()  # the old tree was replaced, not merged into


def test_update_git_registers_the_new_record_before_purging_the_old_tree(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.git.next_commit = "v1"
    harness.git.on_clone = lambda dest, *_: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    harness.git.next_commit = "v2"
    registered: list[AppRecord] = []
    from palmimo_portal.core import apps_jobs

    original_purge = apps_jobs.purge_path

    def delayed_purge(ctx: AppsJobContext, path: Path) -> str | None:
        if path.parent == ctx.trash_dir:
            assert registered[-1].source.commit == "v2"
            assert registered[-1].manifest is not None
            assert registered[-1].requires_python is None
        return original_purge(ctx, path)

    monkeypatch.setattr(apps_jobs, "purge_path", delayed_purge)
    update_git(harness.ctx, state, record.id, on_registered=registered.append)

    assert registered[-1].source.commit == "v2"


def test_update_git_follows_the_catalog_to_its_latest_tag_for_an_official_devkit_app(harness: Harness) -> None:
    old_source = AppSource(
        type="git",
        url="https://github.com/Jizai-inc/palmimo-devkit",
        ref="examples-v0.1.0",
        ref_kind="tag",
        commit="old-commit",
        subdir="examples/teleop",
    )
    record = AppRecord(
        id="palmimo.teleop",
        name="palmimo-teleop",
        source=old_source,
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
    )
    state = AppsState(apps={record.id: record})
    (harness.ctx.apps_dir / record.id).mkdir(parents=True)

    latest_app = replace(
        make_catalog_app(subdir="examples/teleop", commit="new-commit"),
        source=replace(make_catalog_app(subdir="examples/teleop", commit="new-commit").source, ref="examples-v0.1.1"),
    )
    catalog = CatalogCache(
        FakeCatalogSource(asset=CatalogAsset(tag="examples-v0.1.1", apps=(latest_app,))), FakeStateStore()
    )
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")
    harness.git.remote_commits[("https://github.com/Jizai-inc/palmimo-devkit", "examples-v0.1.1", "tag")] = "new-commit"
    harness.git.on_clone = lambda dest, *_: _seed_git_clone(dest / "examples" / "teleop", "palmimo-teleop")

    _, new_record = update_git(ctx, state, record.id)

    assert new_record.source.ref == "examples-v0.1.1"
    assert new_record.source.commit == "new-commit"

    available, _ = check_git_update(ctx, new_record)
    assert available is False


def test_update_git_accepts_a_manifest_name_change_and_keeps_the_id(harness: Harness) -> None:
    # Without this, an author renaming their app upstream would either be rejected outright or
    # silently reassigned a new id, breaking every binding/param/autostart flag the id owns.
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")

    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop-renamed")
    _, new_record = update_git(harness.ctx, state, record.id)

    assert new_record.id == record.id
    assert new_record.name == "palmimo-teleop-renamed"


def test_update_git_fails_at_swap_when_a_start_slips_in_after_prechecks(harness: Harness) -> None:
    from dataclasses import replace

    from palmimo_portal.core.apps_jobs import AppRunningAtSwapError
    from palmimo_portal.ports import UnitStatus

    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    original_contents = sorted((harness.ctx.apps_dir / record.id).rglob("*"))

    app_unit = FakeAppUnitPort()
    state_store = FakeStateStore()
    ctx = replace(harness.ctx, app_unit=app_unit, state=state_store)
    # A start that raced ahead of the job's own (already-passed) precheck -- the unit is now
    # occupied by the time update_git reaches its swap.
    app_unit.simulate_active_state(
        record.id, UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    with pytest.raises(AppRunningAtSwapError):
        update_git(ctx, state, record.id)

    assert sorted((harness.ctx.apps_dir / record.id).rglob("*")) == original_contents


def test_update_git_leaves_running_tree_untouched_when_sync_fails(harness: Harness) -> None:
    harness.git.on_clone = lambda dest, url, ref, ref_kind: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    original_contents = sorted((harness.ctx.apps_dir / record.id).rglob("*"))

    harness.sync_unit.default_status = UnitStatus(
        active_state="failed", sub_state="failed", result="exit-code", exec_main_status=1
    )
    with pytest.raises(SyncFailedError):
        update_git(harness.ctx, state, record.id)

    assert sorted((harness.ctx.apps_dir / record.id).rglob("*")) == original_contents


def test_update_git_reuses_and_restores_the_apps_uv_cache(harness: Harness) -> None:
    harness.git.on_clone = lambda dest, *_: _seed_git_clone(dest, "palmimo-teleop")
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    app_cache = harness.ctx.uv_cache_dir / record.id
    app_cache.mkdir(parents=True, exist_ok=True)
    (app_cache / "prior-cache").write_text("reusable")
    seen: list[set[str]] = []
    original_start = harness.sync_unit.start

    def tracking_start(instance: str) -> None:
        seen.append({path.name for path in (harness.ctx.staging_dir / instance / ".uv-cache").iterdir()})
        original_start(instance)

    harness.sync_unit.start = tracking_start  # type: ignore[method-assign]
    update_git(harness.ctx, state, record.id)

    assert seen == [{"prior-cache"}]
    assert (app_cache / "prior-cache").is_file()
    assert not (harness.ctx.apps_dir / record.id / ".uv-cache").exists()


def test_install_git_rejects_a_dangling_symlink_in_the_staging_tree_before_the_swap(harness: Harness) -> None:
    def seed_with_symlink(dest: Path, *_: object) -> None:
        _seed_git_clone(dest, "palmimo-teleop")
        (dest / "evil-link").symlink_to(dest / "does-not-exist")

    harness.git.on_clone = seed_with_symlink

    with pytest.raises(InvalidManifestSourceError):
        install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")

    assert not (harness.ctx.apps_dir / "palmimo-teleop").exists()


def test_prepare_install_git_rejects_an_official_catalog_tag_at_the_wrong_commit(harness: Harness) -> None:
    """A moved tag must not replace the commit that the signed catalog advertised."""
    expected = "catalog-commit"
    catalog = CatalogCache(
        FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=(make_catalog_app(commit=expected),))), FakeStateStore()
    )
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")
    harness.git.next_commit = "moved-tag-commit"
    harness.git.on_clone = lambda dest, *_: _seed_git_clone(dest, "palmimo-teleop")

    with pytest.raises(GitCommandError) as excinfo:
        prepare_install_git(
            ctx,
            url="https://github.com/Jizai-inc/palmimo-devkit",
            ref="v1.0.0",
            ref_kind="tag",
        )

    assert excinfo.value.reason == "git_commit_mismatch"


def test_preview_git_rejects_an_official_catalog_tag_at_the_wrong_commit(harness: Harness) -> None:
    expected = "catalog-commit"
    catalog = CatalogCache(
        FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=(make_catalog_app(commit=expected),))), FakeStateStore()
    )
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")
    harness.git.next_commit = "moved-tag-commit"
    harness.git.on_clone = lambda dest, *_: _seed_git_clone(dest, "palmimo-teleop")

    with pytest.raises(GitCommandError) as excinfo:
        preview_git(ctx, url="https://github.com/Jizai-inc/palmimo-devkit", ref="v1.0.0", ref_kind="tag")

    assert excinfo.value.reason == "git_commit_mismatch"


def test_prepare_install_git_uses_blobless_sparse_clone_for_an_official_catalog_subdir(harness: Harness) -> None:
    catalog = CatalogCache(
        FakeCatalogSource(asset=CatalogAsset(tag="v1.0.0", apps=(make_catalog_app(subdir="examples/teleop"),))),
        FakeStateStore(),
    )
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")

    def seed(dest: Path, *_: object) -> None:
        _seed_git_clone(dest / "examples" / "teleop", "palmimo-teleop")

    harness.git.on_clone = seed
    prepare_install_git(
        ctx,
        url="https://github.com/Jizai-inc/palmimo-devkit",
        ref="v1.0.0",
        ref_kind="tag",
        subdir="examples/teleop",
    )

    assert harness.git.clone_options == [(True, "examples/teleop")]


def test_install_git_leaves_an_unwritten_symlink_outside_the_app_subdir_alone(harness: Harness) -> None:
    def seed(dest: Path, *_: object) -> None:
        _seed_git_clone(dest / "app", "palmimo-teleop")
        dest.mkdir(exist_ok=True)
        (dest / "outside-link").symlink_to(dest / "does-not-exist")

    harness.git.on_clone = seed

    _, record = install_git(
        harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch", subdir="app"
    )

    assert record.source.subdir == "app"


def test_install_git_does_not_follow_a_sync_json_symlink_at_the_clone_root(harness: Harness, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")

    def seed(dest: Path, *_: object) -> None:
        _seed_git_clone(dest / "app", "palmimo-teleop")
        dest.mkdir(exist_ok=True)
        (dest / "sync.json").symlink_to(victim)

    harness.git.on_clone = seed

    with pytest.raises(InvalidManifestSourceError):
        install_git(
            harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch", subdir="app"
        )

    assert victim.read_text(encoding="utf-8") == "untouched"


def test_update_git_leaves_bindings_intact_when_the_swap_fails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed_with_env(dest: Path, *_: object) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text(
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run"]\n'
            '\n[env.API_KEY]\ndescription = "a key"\n'
        )
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    harness.git.on_clone = seed_with_env
    state, record = install_git(harness.ctx, AppsState(), url="https://example.com/repo", ref="main", ref_kind="branch")
    harness.secrets.set_secret("MY_KEY", "value")
    harness.secrets.write_bindings(record.id, {"API_KEY": "MY_KEY"})
    original_bindings = harness.secrets.read_bindings(record.id)

    def seed_without_env(dest: Path, *_: object) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "palmimo.toml").write_text(
            'schema = 1\nname = "palmimo-teleop"\ndescription = "d"\ncommand = ["run"]\n'
        )
        (dest / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")

    harness.git.on_clone = seed_without_env

    def raise_on_rename(self: Path, target: object) -> Path:
        raise OSError("swap failed")

    monkeypatch.setattr(Path, "rename", raise_on_rename)

    with pytest.raises(OSError):
        update_git(harness.ctx, state, record.id)

    assert harness.secrets.read_bindings(record.id) == original_bindings


def test_update_git_on_non_git_app_is_refused(harness: Harness) -> None:
    state, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    with pytest.raises(InvalidManifestSourceError):
        update_git(harness.ctx, state, record.id)


def test_update_git_on_unknown_app_raises_not_found(harness: Harness) -> None:
    with pytest.raises(AppNotFoundError):
        update_git(harness.ctx, AppsState(), "missing-app")


def test_delete_from_ledger_and_purge_removes_app_directory(harness: Harness) -> None:
    state, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))
    harness.secrets.write_bindings(record.id, {})

    new_state, _ = delete_from_ledger(state, record.id)
    purge_app_files(harness.ctx, record.id)

    assert record.id not in new_state.apps
    assert not (harness.ctx.apps_dir / record.id).exists()


def test_purge_app_files_fails_at_trash_when_a_start_slips_in_after_prechecks(harness: Harness) -> None:
    from dataclasses import replace

    from palmimo_portal.core.apps_jobs import AppRunningAtSwapError
    from palmimo_portal.ports import UnitStatus

    state, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))
    harness.secrets.write_bindings(record.id, {})
    delete_from_ledger(state, record.id)

    app_unit = FakeAppUnitPort()
    ctx = replace(harness.ctx, app_unit=app_unit, state=FakeStateStore())
    app_unit.simulate_active_state(
        record.id, UnitStatus(active_state="active", sub_state="running", result="success", exec_main_status=0)
    )

    with pytest.raises(AppRunningAtSwapError):
        purge_app_files(ctx, record.id)

    assert (harness.ctx.apps_dir / record.id).exists()


def test_purge_app_files_keeps_bindings_when_the_directory_move_fails(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))
    harness.secrets.set_secret("TOKEN", "s3cr3t")
    harness.secrets.write_bindings(record.id, {"TOKEN": "TOKEN"})
    dest = harness.ctx.app_dir(record.id)
    original_rename = Path.rename

    def failing_rename(self: Path, target: str | Path) -> Path:
        if self == dest:
            raise OSError("simulated rename failure")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)

    with pytest.raises(OSError, match="simulated rename failure"):
        purge_app_files(harness.ctx, record.id)

    assert dest.exists()
    assert harness.secrets.read_bindings(record.id) == {"TOKEN": "TOKEN"}


def test_delete_from_ledger_unknown_app_raises_not_found() -> None:
    with pytest.raises(AppNotFoundError):
        delete_from_ledger(AppsState(), "missing-app")


def test_purge_app_files_removes_the_deleted_apps_uv_cache(harness: Harness) -> None:
    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))
    app_cache = harness.ctx.uv_cache_dir / record.id
    (app_cache / "artifact").write_text("cached")

    purge_app_files(harness.ctx, record.id)

    assert not app_cache.exists()


def test_sweep_orphan_app_dirs_removes_unledgered_directory_and_allows_reinstall(harness: Harness) -> None:
    orphan = harness.ctx.apps_dir / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "palmimo.toml").write_text("stale")

    sweep_orphan_app_dirs(harness.ctx, AppsState())

    assert not orphan.exists()
    _, record = install_zip(harness.ctx, AppsState(), _zip_bytes("orphan"))
    assert record.name == "orphan"


def test_sweep_orphan_app_dirs_leaves_ledgered_app_directory(harness: Harness) -> None:
    state, record = install_zip(harness.ctx, AppsState(), _zip_bytes("palmimo-teleop"))

    sweep_orphan_app_dirs(harness.ctx, state)

    assert (harness.ctx.apps_dir / record.id / "palmimo.toml").is_file()


def _tag_record(url: str, ref: str) -> AppRecord:
    return AppRecord(
        name="palmimo-teleop",
        source=AppSource(type="git", url=url, ref=ref, ref_kind="tag"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
    )


def test_check_git_update_reports_update_available_for_an_official_devkit_tag_behind_the_catalog(
    harness: Harness,
) -> None:
    catalog = CatalogCache(FakeCatalogSource(asset=CatalogAsset(tag="v0.3.0", apps=())), FakeStateStore())
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")
    record = _tag_record("https://github.com/Jizai-inc/palmimo-devkit", "v0.2.0")

    available, latest = check_git_update(ctx, record)

    assert (available, latest) == (True, "v0.3.0")


def test_check_git_update_reports_no_update_for_a_tag_pinned_app_from_a_non_official_repo(harness: Harness) -> None:
    catalog = CatalogCache(FakeCatalogSource(asset=CatalogAsset(tag="v0.3.0", apps=())), FakeStateStore())
    catalog.get(ntp_synchronized=True)
    ctx = replace(harness.ctx, catalog_cache=catalog, catalog_repo="Jizai-inc/palmimo-devkit")
    record = _tag_record("https://github.com/someone-else/fork", "v0.2.0")

    available, latest = check_git_update(ctx, record)

    assert (available, latest) == (False, None)
