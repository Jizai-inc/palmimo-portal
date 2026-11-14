from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.display_assets import AppEnvironment, DisplayAssetConverter, StandardAssets
from palmimo_portal.adapters.platform import SudoPlatformPort
from palmimo_portal.ports import DisplayBusyError, DisplayToolError, PlatformCommandError


def invoke(port: SudoPlatformPort, operation: str) -> Any:
    match operation:
        case "desire":
            return port.desire_display_firmware(2, "app-start")
        case "sync" | "sync-standard":
            return port.sync_display_assets("app.example" if operation == "sync" else None)
        case "replace":
            return port.replace_display_assets("app.example", Path("/stage"))
        case "uninstall":
            return port.uninstall_display_assets("app.example")
        case "override" | "reset":
            return port.override_display_asset("happy", Path("/new.bin") if operation == "override" else None)
        case "status":
            return port.display_assets_status()
    raise AssertionError(operation)


@pytest.mark.parametrize(
    ("operation", "arguments", "budget", "payload"),
    [
        ("desire", ["desire", "2", "--by", "app-start", "--json"], 10, {"protocol": 2, "changed": True}),
        (
            "sync",
            ["assets", "sync", "--app", "app.example", "--json"],
            300,
            {"result": "synced", "put": ["palmimo/happy"]},
        ),
        ("sync-standard", ["assets", "sync", "--json"], 300, {"result": "skipped"}),
        ("replace", ["assets", "replace", "app.example", "/stage", "--json"], 150, {"ok": True}),
        ("uninstall", ["assets", "uninstall", "app.example", "--json"], 150, {"ok": True}),
        ("override", ["assets", "override", "happy", "/new.bin", "--json"], 150, {"ok": True}),
        ("reset", ["assets", "override", "--reset", "happy", "--json"], 150, {"ok": True}),
        (
            "status",
            ["assets", "status", "--json"],
            150,
            {"capacity": 6275072, "standard": [], "apps": {}, "orphan_overrides": []},
        ),
    ],
)
def test_platform_display_commands_preserve_arguments_and_results(
    tmp_path: Path, operation: str, arguments: list[str], budget: int, payload: dict[str, Any]
) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        assert argv[5] == str(budget)
        assert argv[7:] == arguments
        assert options["timeout"] > budget + 5
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    result = invoke(SudoPlatformPort(tmp_path / "installed", runner=run), operation)
    if operation == "desire":
        assert (result.protocol, result.changed) == (2, True)
    elif operation.startswith("sync"):
        assert result.result == payload["result"]
        assert result.put == tuple(payload.get("put", []))
    elif operation == "status":
        assert result.capacity == 6275072
    else:
        assert result is None


@pytest.mark.parametrize("operation", ["desire", "replace", "uninstall", "override", "reset", "status"])
@pytest.mark.parametrize("code", [75, 1, 2, 0])
def test_platform_display_failures_preserve_busy_and_capacity(tmp_path: Path, operation: str, code: int) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, '{"ok":false,"reason":"full","short_by":4096}', "bad usage")

    expected = {75: DisplayBusyError, 1: DisplayToolError, 2: PlatformCommandError, 0: DisplayToolError}[code]
    with pytest.raises(expected) as caught:
        invoke(SudoPlatformPort(tmp_path / "installed", runner=run), operation)
    if isinstance(caught.value, DisplayToolError):
        assert (caught.value.reason, caught.value.short_by) == ("full", 4096)


@pytest.mark.parametrize(("code", "outcome"), [(0, "synced"), (75, "busy"), (1, "failed")])
def test_platform_sync_returns_recoverable_outcomes(tmp_path: Path, code: int, outcome: str) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, json.dumps({"result": outcome, "reason": "detail"}), "")

    result = SudoPlatformPort(tmp_path / "installed", runner=run).sync_display_assets(None)
    assert (result.result, result.reason) == (outcome, "detail")


@pytest.mark.parametrize("output", ["{}\n{}", "not json", "[]", '{"ok":true}', '{"protocol":true,"changed":true}'])
def test_platform_rejects_invalid_display_output(tmp_path: Path, output: str) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(PlatformCommandError):
        SudoPlatformPort(tmp_path / "installed", runner=run).desire_display_firmware(2, "initial")


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"schema": 1, "protocol": True, "by": "initial"}, {"schema": 1, "protocol": 2, "by": "initial"}],
)
def test_selection_reads_only_valid_declarations(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "desired.json"
    if payload is not None:
        path.write_text(json.dumps(payload))
    result = SudoPlatformPort(
        tmp_path / "installed", display_firmware_selection_path=path
    ).read_display_firmware_selection()
    if payload == {"schema": 1, "protocol": 2, "by": "initial"}:
        assert result is not None and (result.protocol, result.by) == (2, "initial")
    else:
        assert result is None


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "undeclared",
        "symlink",
        "parent-symlink",
        "oversized",
        "multiple",
        "invalid",
        "valid",
        "unrecorded",
        "broken-json",
        "missing-record",
        "duplicate-record",
    ],
)
def test_environment_reads_installed_declaration_safely(tmp_path: Path, case: str) -> None:
    site = tmp_path / "lib/python3.12/site-packages"
    dist = site / "palmimo_sdk-1.0.dist-info"
    if case != "absent":
        dist.mkdir(parents=True)
        (dist / "RECORD").write_text("palmimo_sdk/display_firmware.json,,\n" if case != "unrecorded" else "other,,\n")
        package = site / "palmimo_sdk"
        package.mkdir()
        declaration = package / "display_firmware.json"
        if case != "undeclared":
            declaration.write_text('{"schema":1,"protocol":2}' if case != "invalid" else '{"schema":1,"protocol":true}')
        if case == "symlink":
            declaration.rename(package / "real.json")
            declaration.symlink_to(package / "real.json")
        elif case == "parent-symlink":
            package.rename(site / "real")
            package.symlink_to(site / "real", target_is_directory=True)
        elif case == "oversized":
            declaration.write_text('{"schema":1,"protocol":2}' + " " * 4096)
        elif case == "multiple":
            (site / "palmimo_sdk-2.0.dist-info").mkdir()
        elif case == "broken-json":
            declaration.write_text("{")
        elif case == "missing-record":
            (dist / "RECORD").unlink()
        elif case == "duplicate-record":
            (dist / "RECORD").write_text("palmimo_sdk/display_firmware.json,,\n" * 2)
    expected = "none" if case == "absent" else "2" if case == "valid" else "1"
    assert AppEnvironment().read_display_firmware_need(tmp_path) == expected


@pytest.mark.parametrize("bad", ["Bad.bin", "include.bin", "link.bin", "directory.bin", None])
def test_standard_names_validate_entire_inventory(tmp_path: Path, bad: str | None) -> None:
    for name in ("happy.bin", "system.idle.bin"):
        (tmp_path / name).write_bytes(b"data")
    (tmp_path / "catalog.json").write_text('{"assets":[]}')
    if bad == "link.bin":
        (tmp_path / bad).symlink_to(tmp_path / "happy.bin")
    elif bad == "directory.bin":
        (tmp_path / bad).mkdir()
    elif bad:
        (tmp_path / bad).write_bytes(b"data")
    assets = StandardAssets(tmp_path)
    if bad:
        with pytest.raises(DisplayToolError):
            assets.list_standard_names()
    else:
        assert assets.list_standard_names() == ("happy", "system.idle")
        assert assets.read_catalog() == {"assets": []}


@pytest.mark.parametrize("operation", ["convert", "inspect"])
@pytest.mark.parametrize("ok", [True, False])
def test_converter_preserves_metadata_and_diagnostics(operation: str, ok: bool) -> None:
    payload: dict[str, Any] = (
        {
            "ok": True,
            "kind": "vector",
            "bytes": 80,
            "crc32": "1234abcd",
            "params": {"color": {"dim": 4}},
            "warnings": [{"code": "slow", "message": "slow animation"}],
        }
        if ok
        else {"ok": False, "errors": [{"code": "unsupported", "path": "layers[0]", "message": "unsupported layer"}]}
    )

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        expected = (
            ["convert", "/in", "-o", "/out", "--json"] if operation == "convert" else ["inspect", "/in", "--json"]
        )
        assert argv[1:] == expected
        assert options["timeout"] == 60
        return subprocess.CompletedProcess(argv, 0 if ok else 2, json.dumps(payload), "")

    converter = DisplayAssetConverter(runner=run)
    result = converter.convert(Path("/in"), Path("/out")) if operation == "convert" else converter.inspect(Path("/in"))
    assert result.ok == ok
    if ok:
        assert (result.kind, result.bytes, result.crc32, result.params) == ("vector", 80, "1234abcd", {"color": 4})
        assert result.warnings[0].code == "slow"
    else:
        assert (result.errors[0].code, result.errors[0].path) == ("unsupported", "layers[0]")


@pytest.mark.parametrize("operation", ["convert", "inspect"])
@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("converter", 60)])
def test_converter_reports_execution_failures(operation: str, failure: Exception) -> None:
    def run(*args: Any, **options: Any) -> Any:
        raise failure

    converter = DisplayAssetConverter(runner=run)
    with pytest.raises(DisplayToolError):
        if operation == "convert":
            converter.convert(Path("/in"), Path("/out"))
        else:
            converter.inspect(Path("/in"))


@pytest.mark.parametrize(
    ("code", "output"),
    [(1, ""), (0, "{}\n{}"), (0, "null"), (0, '{"ok":true}'), (2, '{"ok":false,"errors":[{}]}'), (0, '{"ok":false}')],
)
def test_converter_rejects_invalid_results(code: int, output: str) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, output, "")

    with pytest.raises(DisplayToolError):
        DisplayAssetConverter(runner=run).inspect(Path("/in"))


@pytest.mark.parametrize("operation", ["desire", "sync", "replace", "uninstall", "override", "status"])
@pytest.mark.parametrize("failure", [OSError("unavailable"), subprocess.TimeoutExpired("tool", 150)])
def test_platform_display_reports_execution_failures(tmp_path: Path, operation: str, failure: Exception) -> None:
    def run(*args: Any, **options: Any) -> Any:
        raise failure

    with pytest.raises(PlatformCommandError):
        invoke(SudoPlatformPort(tmp_path / "installed", runner=run), operation)


def test_environment_allows_symlink_ancestors_above_venv(tmp_path: Path) -> None:
    ancestor = tmp_path / "real"
    venv = ancestor / "venv"
    site = venv / "lib/python3.12/site-packages"
    dist = site / "palmimo_sdk-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "RECORD").write_text("palmimo_sdk/display_firmware.json,,\n")
    package = site / "palmimo_sdk"
    package.mkdir()
    (package / "display_firmware.json").write_text('{"schema":1,"protocol":2}')
    alias = tmp_path / "alias"
    alias.symlink_to(ancestor, target_is_directory=True)
    assert AppEnvironment().read_display_firmware_need(alias / "venv") == "2"


@pytest.mark.parametrize("output", ["", "not json", "{}\n{}", "[]"])
def test_platform_display_invalid_output_preserves_stderr_tail(tmp_path: Path, output: str) -> None:
    stderr = "prefix" * 100 + "tool diagnostic"

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, output, stderr)

    with pytest.raises(PlatformCommandError) as caught:
        SudoPlatformPort(tmp_path / "installed", runner=run).sync_display_assets(None)
    assert str(caught.value).endswith(stderr[-500:])
    assert stderr not in str(caught.value)


def test_platform_display_provides_stderr_progress_callback(tmp_path: Path) -> None:
    callbacks = []

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        callback = options.get("on_stderr")
        callbacks.append(callback)
        if callable(callback):
            callback("progress")
        return subprocess.CompletedProcess(argv, 0, '{"result":"synced"}', "")

    assert SudoPlatformPort(tmp_path / "installed", runner=run).sync_display_assets(None).result == "synced"
    assert len(callbacks) == 1 and callable(callbacks[0])


def test_platform_desire_preserves_tool_failure_payload(tmp_path: Path) -> None:
    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, '{"result":"failed","reason":"unavailable"}', "")

    with pytest.raises(DisplayToolError) as caught:
        SudoPlatformPort(tmp_path / "installed", runner=run).desire_display_firmware(2, "initial")
    assert caught.value.reason == "unavailable"
    assert caught.value.short_by is None
