import json
from pathlib import Path

import pytest

from palmimo_portal.adapters.rundir import TmpfsRunDirPort
from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest
from palmimo_portal.ports import AppJob, AppRecord, AppSource, AppsState, DisplayFirmwareNeed, DisplaySyncPending
from palmimo_portal.testing.fakes import FakeStateStore


@pytest.mark.parametrize("fake", [False, True])
def test_display_sync_pending_survives_store_reads_and_clears(tmp_path: Path, fake: bool) -> None:
    store = FakeStateStore() if fake else JsonFileStateStore(tmp_path)
    assert store.read_display_sync() is None
    pending = DisplaySyncPending(pending=True, reason="display locked", boot_id="boot-a")
    store.write_display_sync(pending)
    reader = store if fake else JsonFileStateStore(tmp_path)
    assert reader.read_display_sync() == pending
    reader.write_display_sync(None)
    assert store.read_display_sync() is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "{}",
        '{"schema": 2, "pending": true, "reason": "busy", "boot_id": "boot"}',
        '{"schema": 1, "pending": "true", "reason": "busy", "boot_id": "boot"}',
        '{"schema": 1, "pending": true, "reason": 3, "boot_id": "boot"}',
        '{"schema": 1, "pending": true, "reason": "busy", "boot_id": []}',
    ],
)
def test_display_sync_pending_ignores_unreadable_payload(tmp_path: Path, text: str) -> None:
    (tmp_path / "display_sync.json").write_text(text)
    assert JsonFileStateStore(tmp_path).read_display_sync() is None


@pytest.mark.parametrize("need", [None, "none", "1", "2"])
def test_app_ledger_preserves_display_start_fields(tmp_path: Path, need: DisplayFirmwareNeed | None) -> None:
    job = AppJob("job", "start", "done", "launch", None, 1, 2, boot_id="boot", warning="absent")
    record = AppRecord(
        name="app",
        id="test.app",
        source=AppSource("zip"),
        installed_at=0,
        params={},
        autostart=False,
        last_job=job,
        display_firmware=need,
        manifest=manifest_snapshot(parse_manifest('schema = 1\nname = "app"\ndescription = "app"\ncommand = ["run"]')),
    )
    state = AppsState(apps={"test.app": record}, current_job=job, current_job_app="test.app")
    JsonFileStateStore(tmp_path).write_apps_state(state)
    assert JsonFileStateStore(tmp_path).read_apps_state() == state


def test_run_directory_assets_are_readable_and_removed_on_next_start(tmp_path: Path) -> None:
    store = TmpfsRunDirPort(tmp_path)
    assets = json.dumps({"welcome": "app/welcome"})
    store.write("app", env={}, argv=["run"], cwd="/app", project="/app", assets=assets)
    path = tmp_path / "app" / "assets.json"
    assert path.read_text() == assets
    assert path.stat().st_mode & 0o777 == 0o640
    store.write("app", env={}, argv=["run"], cwd="/app", project="/app")
    assert not path.exists()
