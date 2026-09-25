"""Behavioral tests for the app ledger: corrupt handling and startup finalize."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.core.apps import clear_credential_rejected, finalize_apps_state
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest
from palmimo_portal.ports import AppJob, AppRecord, AppSource, AppsState, AppsStateFileState


APP_ID = "palmimo.teleop"

_MANIFEST = manifest_snapshot(
    parse_manifest('schema = 1\nname = "app"\ndescription = "app"\ncommand = ["run"]')
)


def _record(
    name: str,
    *,
    broken_reason: str | None = None,
    last_job: AppJob | None = None,
    url: str = "https://example.com/repo",
    credential_rejected: bool = False,
    update_available: bool = False,
    latest_commit: str | None = None,
) -> AppRecord:
    return AppRecord(
        name=name,
        source=AppSource(type="git", url=url, ref="main", ref_kind="branch"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=last_job,
        broken_reason=broken_reason,
        credential_rejected=credential_rejected,
        update_available=update_available,
        latest_commit=latest_commit,
        manifest=_MANIFEST,
        id=APP_ID,
    )


def test_apps_state_file_state_reports_corrupt_when_file_is_unparseable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "apps.json").write_text("not json", encoding="utf-8")
    store = JsonFileStateStore(state_dir)

    assert store.apps_state_file_state() is AppsStateFileState.CORRUPT


def test_apps_state_file_state_reports_legacy_for_a_legacy_name_key(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "apps.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "apps": {"teleop": {"name": "teleop", "source": {"type": "zip"}}},
                "current_job": None,
                "current_job_app": None,
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(state_dir)

    assert store.apps_state_file_state() is AppsStateFileState.LEGACY


def test_apps_state_file_state_reports_corrupt_when_an_id_record_has_a_legacy_shaped_key(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "apps.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "apps": {"teleop": {"id": APP_ID, "name": "teleop", "source": {"type": "zip"}}},
                "current_job": None,
                "current_job_app": None,
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(state_dir)

    assert store.apps_state_file_state() is AppsStateFileState.CORRUPT


def test_read_apps_state_does_not_silently_lose_installed_apps_when_corrupt(tmp_path: Path) -> None:
    # A caller that skips apps_state_file_state() and reads a corrupt ledger
    # as plain AppsState() sees zero apps -- this test pins that
    # read_apps_state() alone cannot be used to tell "no apps" from "corrupt".
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "apps.json").write_text("not json", encoding="utf-8")
    store = JsonFileStateStore(state_dir)

    assert store.apps_state_file_state() is AppsStateFileState.CORRUPT
    assert store.read_apps_state() == AppsState()


def test_write_then_read_apps_state_round_trips(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "state")
    job = AppJob(id="j1", kind="install", state="done", step="register", error=None, started_at=1.0, finished_at=2.0)
    record = _record("palmimo-teleop", last_job=job)
    state = AppsState(apps={APP_ID: record})

    store.write_apps_state(state)

    assert store.read_apps_state() == state
    assert store.apps_state_file_state() is AppsStateFileState.PRESENT


def test_write_then_read_apps_state_round_trips_periodic_check_fields(tmp_path: Path) -> None:
    # Without this, a fresh JsonFileStateStore instance (a Portal restart) would silently
    # forget every periodic git-check result: update_available/latest_commit reset to their
    # defaults, and a rejected credential would resume being checked as if never rejected.
    store = JsonFileStateStore(tmp_path / "state")
    record = _record("palmimo-teleop", credential_rejected=True, update_available=True, latest_commit="deadbeef")
    state = AppsState(apps={APP_ID: record})

    store.write_apps_state(state)

    assert store.read_apps_state() == state


def test_write_then_read_apps_state_round_trips_a_non_default_manifest(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "state")
    record = replace(_record("palmimo-realtime"), id="palmimo.realtime")
    record = replace(record, source=replace(record.source, manifest="palmimo.realtime.toml"))
    state = AppsState(apps={"palmimo.realtime": record})

    store.write_apps_state(state)

    assert store.read_apps_state() == state


def test_apps_state_file_state_reports_corrupt_when_an_id_ledger_entry_has_no_manifest_snapshot(tmp_path: Path) -> None:
    # Reading a mutable checkout during start would let its author add devices
    # after installation, so persisted records without the validated snapshot
    # are intentionally not accepted as installed apps.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "apps.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "apps": {
                    APP_ID: {
                        "name": "palmimo-teleop",
                        "source": {
                            "type": "git",
                            "url": "https://example.com/repo",
                            "ref": "main",
                            "ref_kind": "branch",
                            "subdir": None,
                            "commit": None,
                        },
                        "installed_at": 1.0,
                        "params": {},
                        "autostart": False,
                        "last_job": None,
                    }
                },
                "current_job": None,
                "current_job_app": None,
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(state_dir)

    assert store.apps_state_file_state() is AppsStateFileState.CORRUPT


def test_write_then_read_apps_state_round_trips_the_orphan_job(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path / "state")
    job = AppJob(id="j1", kind="install", state="failed", step="sync", error="boom", started_at=1.0, finished_at=2.0)
    state = AppsState(last_orphan_job=job, last_orphan_job_app=APP_ID)

    store.write_apps_state(state)

    assert store.read_apps_state() == state


def test_clear_credential_rejected_unmarks_only_apps_scoped_to_host_owner() -> None:
    same_owner = _record("app-a", url="https://github.com/acme/repo-a", credential_rejected=True)
    other_owner = _record("app-b", url="https://github.com/other/repo-b", credential_rejected=True)
    state = AppsState(
        apps={"acme.app-a": replace(same_owner, id="acme.app-a"), "other.app-b": replace(other_owner, id="other.app-b")}
    )

    cleared = clear_credential_rejected(state, "github.com/acme")

    assert cleared.apps["acme.app-a"].credential_rejected is False
    assert cleared.apps["other.app-b"].credential_rejected is True


def test_finalize_apps_state_marks_interrupted_job_failed() -> None:
    running_job = AppJob(
        id="j1", kind="update", state="running", step="sync", error=None, started_at=1.0, finished_at=None
    )
    record = _record("palmimo-teleop")
    state = AppsState(apps={APP_ID: record}, current_job=running_job, current_job_app=APP_ID)

    finalized = finalize_apps_state(state, manifest_exists=lambda name: True, venv_exists=lambda name: True, now=99.0)

    assert finalized.current_job is None
    assert finalized.current_job_app is None
    last_job = finalized.apps[APP_ID].last_job
    assert last_job is not None
    assert last_job.state == "failed"
    assert last_job.error == "interrupted"


@pytest.mark.parametrize(
    ("manifest_exists", "venv_exists", "expected_reason"),
    [
        (lambda name: False, lambda name: True, "manifest_missing"),
        (lambda name: True, lambda name: False, "venv_missing"),
    ],
)
def test_finalize_apps_state_marks_broken_when_disk_is_inconsistent(
    manifest_exists: Callable[[str], bool], venv_exists: Callable[[str], bool], expected_reason: str
) -> None:
    state = AppsState(apps={APP_ID: _record("palmimo-teleop")})

    finalized = finalize_apps_state(state, manifest_exists=manifest_exists, venv_exists=venv_exists, now=1.0)

    assert finalized.apps[APP_ID].broken_reason == expected_reason


def test_finalize_apps_state_clears_broken_reason_once_disk_is_consistent_again() -> None:
    state = AppsState(apps={APP_ID: _record("palmimo-teleop", broken_reason="venv_missing")})

    finalized = finalize_apps_state(state, manifest_exists=lambda name: True, venv_exists=lambda name: True, now=1.0)

    assert finalized.apps[APP_ID].broken_reason is None
