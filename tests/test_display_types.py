from typing import Any

import pytest

from palmimo_portal.ports import (
    AppAssetsStatus,
    DisplayAssetsStatus,
    DisplayFirmwareResult,
    DisplaySyncResult,
    StandardAssetStatus,
)


def test_display_firmware_result_accepts_undesired() -> None:
    result = DisplayFirmwareResult.from_payload({"result": "undesired", "reason": "no selection"})
    assert result == DisplayFirmwareResult("undesired", reason="no selection")


@pytest.mark.parametrize(
    ("outcome", "has_assets"), [("synced", True), ("busy", False), ("skipped", False), ("failed", False)]
)
def test_display_sync_result_parses_tool_payload(outcome: str, has_assets: bool) -> None:
    payload: dict[str, Any] = {"result": outcome, "reason": "detail"}
    if has_assets:
        payload.update(put=["palmimo/happy"], deleted=["app/old"])
    result = DisplaySyncResult.from_payload(payload)
    assert result.result == outcome
    assert result.put == (("palmimo/happy",) if has_assets else ())
    assert result.deleted == (("app/old",) if has_assets else ())
    assert result.reason == "detail"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"result": "updated"},
        {"result": []},
        {"result": "synced", "put": "palmimo/happy"},
        {"result": "synced", "put": [1]},
        {"result": "synced", "deleted": [None]},
        {"result": "busy", "reason": 1},
    ],
)
def test_display_sync_result_rejects_invalid_tool_payload(payload: object) -> None:
    with pytest.raises(ValueError):
        DisplaySyncResult.from_payload(payload)


def _status_payload() -> dict[str, Any]:
    return {
        "capacity": 6275072,
        "standard": [
            {
                "name": "happy",
                "bytes": 4000,
                "kind": "image",
                "params": [],
                "overridden": True,
                "default_bytes": 1000,
                "default_kind": "vector",
                "default_params": ["eye_color"],
            }
        ],
        "apps": {"app": {"bytes": 12288, "largest": 8192, "names": ["welcome", "bye"]}},
        "orphan_overrides": ["retired"],
    }


@pytest.mark.parametrize("empty", [False, True])
def test_display_assets_status_preserves_effective_and_default_assets(empty: bool) -> None:
    if empty:
        assert DisplayAssetsStatus.from_payload(
            {"capacity": 6275072, "standard": [], "apps": {}, "orphan_overrides": []}
        ) == DisplayAssetsStatus(6275072, (), {}, ())
        return
    assert DisplayAssetsStatus.from_payload(_status_payload()) == DisplayAssetsStatus(
        6275072,
        (StandardAssetStatus("happy", 4000, "image", (), True, 1000, "vector", ("eye_color",)),),
        {"app": AppAssetsStatus(12288, 8192, ("welcome", "bye"))},
        ("retired",),
    )


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("root", "capacity", True),
        ("root", "capacity", -1),
        ("root", "standard", {}),
        ("root", "apps", []),
        ("root", "apps", {"app": None}),
        ("root", "standard", [None]),
        ("root", "orphan_overrides", "retired"),
        ("standard", "kind", "video"),
        ("standard", "default_kind", "video"),
        ("standard", "name", 1),
        ("standard", "bytes", "4000"),
        ("standard", "default_bytes", False),
        ("standard", "params", [1]),
        ("standard", "default_params", {}),
        ("standard", "overridden", 1),
        ("app", "bytes", -1),
        ("app", "largest", True),
        ("app", "names", [False]),
    ],
)
def test_display_assets_status_rejects_invalid_nested_payload(section: str, key: str, value: object) -> None:
    payload = _status_payload()
    target = (
        payload if section == "root" else payload["standard"][0] if section == "standard" else payload["apps"]["app"]
    )
    target[key] = value
    with pytest.raises(ValueError):
        DisplayAssetsStatus.from_payload(payload)


@pytest.mark.parametrize("payload", [None, [], {}, {"ok": False, "reason": "unreadable"}])
def test_display_assets_status_rejects_incomplete_payload(payload: object) -> None:
    with pytest.raises(ValueError):
        DisplayAssetsStatus.from_payload(payload)
