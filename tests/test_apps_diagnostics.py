"""Behavioral tests for `core/apps_diagnostics.py`: the `GET /apps/{name}/diagnostics` document."""

from __future__ import annotations

from palmimo_portal.core.apps_diagnostics import SECTIONS, AppDiagnosticsInput, build_diagnostics
from palmimo_portal.core.apps_start import PrecheckResult
from palmimo_portal.ports import AppRecord, AppSource, UnitStatus


def _data(**overrides: object) -> AppDiagnosticsInput:
    defaults: dict[str, object] = {
        "portal_version": "1.2.3",
        "platform_status": {"installed_version": 1, "required_version": 1, "ready": True, "verify_diffs": []},
        "uv_version": "uv 0.4.0",
        "record": AppRecord(
            name="app",
            source=AppSource(type="git", url="https://example.com/repo", ref="main", ref_kind="branch"),
            installed_at=0.0,
            params={"port": 8080},
            autostart=False,
            last_job=None,
            id="git.app",
        ),
        "manifest": None,
        "manifest_errors": [],
        "bindings": {"API_KEY": "MY_KEY"},
        "precheck": PrecheckResult(ok=True),
        "unit_status": UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0),
        "journal_lines": ["line one"],
        "disk_free_bytes": 1_000_000,
        "ntp_synchronized": True,
        "hostname": "palmimo-abc123",
        "device_id": "PM-0001",
        "portal_update_job_state": "idle",
    }
    defaults.update(overrides)
    return AppDiagnosticsInput(**defaults)  # type: ignore[arg-type]


def test_build_diagnostics_contains_every_fixed_section_header() -> None:
    text = build_diagnostics(_data(), known_secret_values=[])

    for section in SECTIONS:
        assert section in text

    assert "id=git.app" in text


def test_build_diagnostics_never_contains_a_registered_secret_value_or_git_token() -> None:
    text = build_diagnostics(
        _data(journal_lines=["app printed its own key: sekrit-value and token ghp_abc123"]),
        known_secret_values=["sekrit-value", "ghp_abc123"],
    )

    assert "sekrit-value" not in text
    assert "ghp_abc123" not in text


def test_build_diagnostics_shows_bindings_by_name_only() -> None:
    text = build_diagnostics(_data(), known_secret_values=[])

    assert "API_KEY <- MY_KEY" in text


def test_build_diagnostics_reports_journal_unavailable_when_journal_lines_is_none() -> None:
    text = build_diagnostics(_data(journal_lines=None), known_secret_values=[])

    assert "unavailable: journal_permission" in text


def test_build_diagnostics_reports_a_failing_precheck_code_and_names() -> None:
    text = build_diagnostics(
        _data(precheck=PrecheckResult(ok=False, code="env_unbound", names=["API_KEY"])), known_secret_values=[]
    )

    assert "code=env_unbound" in text
    assert "names=['API_KEY']" in text


def test_build_diagnostics_never_contains_userinfo_from_a_git_source_url() -> None:
    record = AppRecord(
        name="app",
        source=AppSource(type="git", url="https://user:token@github.com/example/repo", ref="main", ref_kind="branch"),
        installed_at=0.0,
        params={},
        autostart=False,
        last_job=None,
        id="git.app",
    )

    text = build_diagnostics(_data(record=record), known_secret_values=[])

    assert "user:token@" not in text
    assert "token" not in text
