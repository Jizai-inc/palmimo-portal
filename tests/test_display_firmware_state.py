from pathlib import Path

import pytest

from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.core.platform_update import (
    IDLE_PLATFORM_STATE,
    advance,
    finalize_after_restart,
    mark_done,
    mark_failed,
    start_update,
)
from palmimo_portal.ports import DisplayFirmwareResult, PlatformUpdateState


def test_display_firmware_survives_transitions_and_disk_round_trips(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    result = DisplayFirmwareResult(result="busy", before="legacy", reason="reserved")
    state = PlatformUpdateState(job=start_update(IDLE_PLATFORM_STATE, 2, 1).job, display_firmware=result)
    for step in ("display_firmware", "restart", "restart"):
        store.write_platform_update_state(advance(state, step))
        state = store.read_platform_update_state()
        assert state.display_firmware == result
    assert mark_done(state, 2).display_firmware == result
    assert mark_failed(state, "restart", "unavailable", 2).display_firmware == result
    assert start_update(mark_done(state, 2), 3, 3).display_firmware is None


def test_display_firmware_interruption_finishes_platform_job() -> None:
    state = advance(start_update(IDLE_PLATFORM_STATE, 2, 1), "display_firmware")
    final = finalize_after_restart(state, 2)
    assert final.job.state == "done"
    assert final.job.error is None
    assert final.display_firmware is not None
    assert final.display_firmware.result == "interrupted"


@pytest.mark.parametrize("payload", ["[]", '{"result": "unknown"}', '{"result": "current", "before": 1}'])
def test_display_firmware_rejects_invalid_result(payload: str) -> None:
    import json

    with pytest.raises(ValueError):
        DisplayFirmwareResult.from_payload(json.loads(payload))


def test_preempt_record_survives_store_recreation_and_clears(tmp_path: Path) -> None:
    from palmimo_portal.ports import PreemptedApp

    path = tmp_path / "run" / "preempt.json"
    record = PreemptedApp("face", "display-firmware", False)
    store = JsonFileStateStore(tmp_path / "state", preempt_path=path)
    store.write_preempted_app(record)
    restarted = JsonFileStateStore(tmp_path / "state", preempt_path=path)
    assert restarted.read_preempted_app() == record
    restarted.write_preempted_app(None)
    assert store.read_preempted_app() is None
