"""Tests for :mod:`palmimo_portal.core.update`."""

from __future__ import annotations

from typing import Literal

import pytest

from palmimo_portal.core.update import (
    DEFAULT_RESTART_MAX_AGE_SECONDS,
    IDLE_UPDATE_STATE,
    InvalidReleaseTagError,
    NoPreviousVersionError,
    NoReleaseCheckedError,
    UpdateCheckRateLimitedError,
    UpdateInProgressError,
    UpdateTargetMismatch,
    advance,
    current_update_state,
    expire_stale_restart,
    expire_stale_running,
    finalize_after_restart,
    is_retry_available,
    is_update_available,
    is_valid_release_tag,
    mark_failed,
    mark_restarting,
    record_latest,
    start_apply,
    start_check,
    start_rollback,
)
from palmimo_portal.ports import InstalledVersion, Release, UpdateJob, UpdateState
from palmimo_portal.testing.fakes import FakeStateStore


RELEASE_V2 = Release(
    tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v2"
)


def _running_state(*, kind: Literal["update", "rollback"] = "update", target: str = "v2.0.0") -> UpdateState:
    return UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="running", kind=kind, target=target, step="fetch", error=None, started_at=100.0, finished_at=None
        ),
    )


def test_is_update_available_is_false_without_a_latest_release() -> None:
    assert is_update_available(InstalledVersion(tag="v1.0.0", commit="abc"), None) is False


def test_is_update_available_is_true_when_installed_tag_is_none() -> None:
    assert is_update_available(InstalledVersion(tag=None, commit="abc"), RELEASE_V2) is True


def test_is_update_available_is_true_when_tags_differ() -> None:
    assert is_update_available(InstalledVersion(tag="v1.0.0", commit="abc"), RELEASE_V2) is True


def test_is_update_available_is_false_when_tags_match() -> None:
    assert is_update_available(InstalledVersion(tag="v2.0.0", commit="abc"), RELEASE_V2) is False


def test_start_check_succeeds_from_idle() -> None:
    result = start_check(IDLE_UPDATE_STATE, now=1000.0)

    assert result.job.state == "checking"


@pytest.mark.parametrize("job_state", ["running", "restarting", "checking"])
def test_start_check_raises_update_in_progress_when_a_job_is_active(
    job_state: Literal["running", "restarting", "checking"],
) -> None:
    state = UpdateState(
        latest=None,
        checked_at=None,
        previous_tag=None,
        job=UpdateJob(
            state=job_state, kind="update", target="v2", step=None, error=None, started_at=1.0, finished_at=None
        ),
    )

    with pytest.raises(UpdateInProgressError):
        start_check(state, now=1000.0)


def test_start_check_raises_rate_limited_within_the_window() -> None:
    state = UpdateState(latest=None, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    with pytest.raises(UpdateCheckRateLimitedError) as excinfo:
        start_check(state, now=1010.0)

    assert excinfo.value.retry_after_seconds == pytest.approx(50.0)


def test_start_check_succeeds_once_the_rate_limit_window_has_passed() -> None:
    state = UpdateState(latest=None, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    result = start_check(state, now=1061.0)

    assert result.job.state == "checking"


def test_start_check_succeeds_when_the_wall_clock_stepped_backwards() -> None:
    # A power-cut reboot on a Pi with no RTC: `now` can land before
    # `checked_at`, making elapsed negative. Must not be read as "just
    # checked a moment ago" and rate-limited.
    state = UpdateState(latest=None, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    result = start_check(state, now=500.0)

    assert result.job.state == "checking"


def test_record_latest_stores_the_release_and_returns_the_job_to_idle() -> None:
    state = start_check(IDLE_UPDATE_STATE, now=1000.0)

    result = record_latest(state, RELEASE_V2, now=1000.5)

    assert result.latest == RELEASE_V2
    assert result.checked_at == 1000.5
    assert result.job.state == "idle"


def test_start_apply_succeeds_and_sets_previous_tag_from_installed() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    result = start_apply(state, InstalledVersion(tag="v1.0.0", commit="abc"), "v2.0.0", now=1001.0)

    assert result.job.state == "running"
    assert result.job.kind == "update"
    assert result.job.target == "v2.0.0"
    assert result.previous_tag == "v1.0.0"


def test_start_apply_keeps_the_existing_previous_tag_when_installed_tag_is_none() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v0.9.0", job=IDLE_UPDATE_STATE.job)

    result = start_apply(state, InstalledVersion(tag=None, commit="abc"), "v2.0.0", now=1001.0)

    assert result.previous_tag == "v0.9.0"


def test_start_apply_keeps_the_existing_previous_tag_on_a_retry_after_a_failed_sync() -> None:
    # v1 -> apply v2 fails at sync (HEAD is already on v2 by the time sync
    # fails) -> retry apply v2. installed.tag == target now, so previous_tag
    # must stay "v1.0.0" rather than being overwritten with "v2.0.0" (which
    # would erase the only record of what to roll back to).
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v1.0.0", job=IDLE_UPDATE_STATE.job)

    result = start_apply(state, InstalledVersion(tag="v2.0.0", commit="abc"), "v2.0.0", now=1001.0)

    assert result.previous_tag == "v1.0.0"


def test_start_apply_raises_no_release_checked_when_latest_is_none() -> None:
    with pytest.raises(NoReleaseCheckedError):
        start_apply(IDLE_UPDATE_STATE, InstalledVersion(tag="v1.0.0", commit="abc"), "v2.0.0", now=1001.0)


def test_start_apply_raises_target_mismatch() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    with pytest.raises(UpdateTargetMismatch):
        start_apply(state, InstalledVersion(tag="v1.0.0", commit="abc"), "v3.0.0", now=1001.0)


def test_start_apply_raises_update_in_progress_when_a_job_is_running() -> None:
    state = _running_state()

    with pytest.raises(UpdateInProgressError):
        start_apply(state, InstalledVersion(tag="v1.0.0", commit="abc"), "v2.0.0", now=1001.0)


def test_start_rollback_succeeds() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v1.0.0", job=IDLE_UPDATE_STATE.job)

    result = start_rollback(state, InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)

    assert result.job.state == "running"
    assert result.job.kind == "rollback"
    assert result.job.target == "v1.0.0"


def test_start_rollback_sets_previous_tag_to_the_tag_being_left() -> None:
    # After the rollback lands, the card should offer to go forward again
    # to "v2.0.0" -- not offer to roll back to the version now installed.
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v1.0.0", job=IDLE_UPDATE_STATE.job)

    result = start_rollback(state, InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)

    assert result.previous_tag == "v2.0.0"


def test_start_rollback_keeps_the_existing_previous_tag_when_installed_tag_is_none() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v1.0.0", job=IDLE_UPDATE_STATE.job)

    result = start_rollback(state, InstalledVersion(tag=None, commit="abc"), now=2000.0)

    assert result.previous_tag == "v1.0.0"


def test_start_rollback_keeps_the_existing_previous_tag_when_installed_tag_equals_the_target() -> None:
    # A retry after a failed rollback: HEAD is already on the rollback
    # target from the failed attempt, so installed.tag == target.
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="v1.0.0", job=IDLE_UPDATE_STATE.job)

    result = start_rollback(state, InstalledVersion(tag="v1.0.0", commit="abc"), now=2000.0)

    assert result.previous_tag == "v1.0.0"


def test_start_rollback_raises_no_previous_version() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    with pytest.raises(NoPreviousVersionError):
        start_rollback(state, InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)


def test_start_rollback_raises_update_in_progress_when_a_job_is_running() -> None:
    with pytest.raises(UpdateInProgressError):
        start_rollback(_running_state(), InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)


def test_advance_records_the_step() -> None:
    state = start_apply(
        UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job),
        InstalledVersion(tag="v1.0.0", commit="abc"),
        "v2.0.0",
        now=1001.0,
    )

    result = advance(state, "checkout")

    assert result.job.step == "checkout"
    assert result.job.state == "running"


def test_mark_restarting_transitions_the_job() -> None:
    state = _running_state()

    result = mark_restarting(state, now=200.0)

    assert result.job.state == "restarting"
    assert result.job.target == "v2.0.0"


def test_mark_failed_records_step_and_error_and_keeps_previous_tag() -> None:
    state = _running_state()

    result = mark_failed(state, "sync", "uv sync failed", now=2000.0)

    assert result.job.state == "failed"
    assert result.job.step == "sync"
    assert result.job.error == "uv sync failed"
    assert result.job.finished_at == 2000.0
    assert result.previous_tag == "v1.0.0"


def test_finalize_after_restart_is_a_no_op_when_done() -> None:
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="done", kind="update", target="v2.0.0", step=None, error=None, started_at=100.0, finished_at=100.5
        ),
    )

    result = finalize_after_restart(state, InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)

    assert result is state


def test_finalize_after_restart_marks_done_when_installed_tag_matches_target() -> None:
    state = mark_restarting(_running_state(), now=200.0)

    result = finalize_after_restart(state, InstalledVersion(tag="v2.0.0", commit="def"), now=2000.0)

    assert result.job.state == "done"
    assert result.job.finished_at == 2000.0


def test_finalize_after_restart_marks_failed_when_installed_tag_does_not_match_target() -> None:
    state = mark_restarting(_running_state(), now=200.0)

    result = finalize_after_restart(state, InstalledVersion(tag=None, commit="badcommit"), now=2000.0)

    assert result.job.state == "failed"
    assert result.job.step == "restart"
    assert "badcommit" in (result.job.error or "")
    assert "v2.0.0" in (result.job.error or "")


def test_finalize_after_restart_marks_done_for_a_rollback_job_and_preserves_kind() -> None:
    # finalize_after_restart's "done" promotion is exercised elsewhere only
    # with kind="update" -- a rollback job must resolve to "done" the same
    # way, with kind="rollback" preserved in the result, not silently
    # coerced to "update".
    state = mark_restarting(_running_state(kind="rollback", target="v1.0.0"), now=200.0)

    result = finalize_after_restart(state, InstalledVersion(tag="v1.0.0", commit="abc"), now=2000.0)

    assert result.job.state == "done"
    assert result.job.kind == "rollback"
    assert result.job.target == "v1.0.0"


def test_finalize_after_restart_marks_idle_job_untouched_on_a_normal_boot() -> None:
    result = finalize_after_restart(IDLE_UPDATE_STATE, InstalledVersion(tag="v2.0.0", commit="def"), now=2000.0)

    assert result is IDLE_UPDATE_STATE


def test_finalize_after_restart_fails_a_running_job_left_over_from_before_boot() -> None:
    state = _running_state()

    result = finalize_after_restart(state, InstalledVersion(tag="v1.0.0", commit="abc"), now=2000.0)

    assert result.job.state == "failed"
    assert result.job.step == "fetch"  # _running_state()'s job.step
    assert result.job.error == "interrupted: the Portal restarted before this job finished"
    assert result.job.finished_at == 2000.0
    assert result.job.target == "v2.0.0"


def test_finalize_after_restart_fails_a_running_job_with_no_step_yet_using_start() -> None:
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="running", kind="update", target="v2.0.0", step=None, error=None, started_at=100.0, finished_at=None
        ),
    )

    result = finalize_after_restart(state, InstalledVersion(tag="v1.0.0", commit="abc"), now=2000.0)

    assert result.job.state == "failed"
    assert result.job.step == "start"


def test_finalize_after_restart_fails_a_checking_job_left_over_from_before_boot() -> None:
    state = UpdateState(
        latest=None,
        checked_at=None,
        previous_tag=None,
        job=UpdateJob(
            state="checking", kind="update", target=None, step=None, error=None, started_at=100.0, finished_at=None
        ),
    )

    result = finalize_after_restart(state, InstalledVersion(tag="v1.0.0", commit="abc"), now=2000.0)

    assert result.job.state == "failed"
    assert result.job.step == "start"
    assert result.job.error == "interrupted: the Portal restarted before this job finished"


@pytest.mark.parametrize(
    "tag",
    ["v2.0.0", "v1", "release-2026.01.01", "a", "A1_2.3-4"],
)
def test_is_valid_release_tag_accepts_ordinary_tags(tag: str) -> None:
    assert is_valid_release_tag(tag) is True


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "-v2.0.0",  # leading dash -- would be read as a flag by git/uv
        "v2.0.0.lock",
        "v2..0",
        "a/b",
        "a b",
        "a" * 129,
    ],
)
def test_is_valid_release_tag_rejects_unsafe_tags(tag: str) -> None:
    assert is_valid_release_tag(tag) is False


def test_record_latest_raises_invalid_release_tag_for_an_unsafe_tag() -> None:
    bad_release = Release(
        tag="-v2.0.0", name="bad", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
    )

    with pytest.raises(InvalidReleaseTagError):
        record_latest(IDLE_UPDATE_STATE, bad_release, now=1000.0)


def test_start_apply_raises_invalid_release_tag_for_an_unsafe_target() -> None:
    bad_release = Release(
        tag="-v2.0.0", name="bad", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
    )
    state = UpdateState(latest=bad_release, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)

    with pytest.raises(InvalidReleaseTagError):
        start_apply(state, InstalledVersion(tag="v1.0.0", commit="abc"), "-v2.0.0", now=1001.0)


def test_start_rollback_raises_invalid_release_tag_for_an_unsafe_previous_tag() -> None:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag="-v0.9.0", job=IDLE_UPDATE_STATE.job)

    with pytest.raises(InvalidReleaseTagError):
        start_rollback(state, InstalledVersion(tag="v2.0.0", commit="abc"), now=2000.0)


def test_expire_stale_restart_is_a_no_op_when_not_restarting() -> None:
    state = _running_state()  # job.state == "running", not "restarting"

    result = expire_stale_restart(state, now=100.0 + DEFAULT_RESTART_MAX_AGE_SECONDS + 1)

    assert result is state


def test_expire_stale_restart_is_a_no_op_within_max_age() -> None:
    state = mark_restarting(_running_state(), now=200.0)  # started_at = 100.0, restarting_at = 200.0

    result = expire_stale_restart(state, now=200.0 + DEFAULT_RESTART_MAX_AGE_SECONDS - 1)

    assert result is state


def test_expire_stale_restart_fails_the_job_past_max_age() -> None:
    state = mark_restarting(_running_state(), now=200.0)  # started_at = 100.0, restarting_at = 200.0

    result = expire_stale_restart(state, now=200.0 + DEFAULT_RESTART_MAX_AGE_SECONDS + 1)

    assert result.job.state == "failed"
    assert result.job.step == "restart"
    assert "reboot from the Power screen" in (result.job.error or "")
    assert result.job.target == "v2.0.0"


def test_expire_stale_restart_respects_a_custom_max_age() -> None:
    state = mark_restarting(_running_state(), now=200.0)  # started_at = 100.0, restarting_at = 200.0

    result = expire_stale_restart(state, now=200.0 + 10.0, max_age_seconds=5.0)

    assert result.job.state == "failed"


def test_expire_stale_restart_is_a_no_op_with_no_started_at() -> None:
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="restarting",
            kind="update",
            target="v2.0.0",
            step="fetch",
            error=None,
            started_at=None,
            finished_at=None,
        ),
    )

    result = expire_stale_restart(state, now=100_000.0)

    assert result is state


def test_expire_stale_restart_measures_from_restart_not_from_apply_start() -> None:
    """A slow-but-healthy apply (started_at 11 min ago) must not be expired the instant
    mark_restarting stamps it -- expiry is measured from restarting_at, not started_at.
    """
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="running",
            kind="update",
            target="v2.0.0",
            step="sync",
            error=None,
            started_at=0.0,  # apply began 11 minutes before mark_restarting -- past DEFAULT_RESTART_MAX_AGE_SECONDS
            finished_at=None,
        ),
    )
    restarting_at = 660.0  # 11 minutes after started_at
    state = mark_restarting(state, now=restarting_at)

    result = expire_stale_restart(state, now=restarting_at + 5.0)  # only 5s into the actual restart wait

    assert result is state


def test_expire_stale_restart_falls_back_to_started_at_when_restarting_at_is_absent() -> None:
    """update.json written before restarting_at existed -- expiry still works, from started_at."""
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="restarting",
            kind="update",
            target="v2.0.0",
            step="fetch",
            error=None,
            started_at=100.0,
            finished_at=None,
            restarting_at=None,
        ),
    )

    result = expire_stale_restart(state, now=100.0 + DEFAULT_RESTART_MAX_AGE_SECONDS + 1)

    assert result.job.state == "failed"
    assert result.job.step == "restart"


def test_finalize_after_restart_promotes_an_expired_restart_to_done_when_installed_matches_target() -> None:
    """A legitimately slow apply that expire_stale_restart already gave up on, but whose restart
    then landed anyway, is promoted from failed(step=restart) to done on the next boot's finalize.
    """
    expired = UpdateJob(
        state="failed",
        kind="update",
        target="v2.0.0",
        step="restart",
        error="the Portal did not restart within 10 minutes -- reboot from the Power screen",
        started_at=100.0,
        finished_at=900.0,
        restarting_at=200.0,
    )
    state = UpdateState(latest=RELEASE_V2, checked_at=100.0, previous_tag="v1.0.0", job=expired)

    result = finalize_after_restart(state, InstalledVersion(tag="v2.0.0", commit="def"), now=2000.0)

    assert result.job.state == "done"
    assert result.job.finished_at == 2000.0
    assert result.job.error is None


def test_finalize_after_restart_leaves_an_expired_restart_failed_when_installed_still_mismatches() -> None:
    expired = UpdateJob(
        state="failed",
        kind="update",
        target="v2.0.0",
        step="restart",
        error="the Portal did not restart within 10 minutes -- reboot from the Power screen",
        started_at=100.0,
        finished_at=900.0,
        restarting_at=200.0,
    )
    state = UpdateState(latest=RELEASE_V2, checked_at=100.0, previous_tag="v1.0.0", job=expired)

    result = finalize_after_restart(state, InstalledVersion(tag="v1.0.0", commit="def"), now=2000.0)

    assert result is state


def test_expire_stale_running_is_a_no_op_when_not_running() -> None:
    state = mark_restarting(_running_state(), now=200.0)  # job.state == "restarting", not "running"

    result = expire_stale_running(state, now=1_000_000.0, runner_alive=False)

    assert result is state


def test_expire_stale_running_is_a_no_op_when_the_runner_is_alive_no_matter_how_old_the_job_is() -> None:
    # A legitimately slow but healthy apply (uv sync rebuilding native
    # wheels on a Pi) must never be expired just because it has been
    # running a long time -- liveness, not wall-clock age, is the only
    # signal that matters. `now` here is deliberately absurd (a huge fake
    # "now") to prove age plays no role at all.
    state = _running_state()  # started_at = 100.0

    result = expire_stale_running(state, now=1e15, runner_alive=True)

    assert result is state


def test_expire_stale_running_fails_the_job_when_the_runner_is_not_alive() -> None:
    # Even a job that only just started must be expired if no live runner
    # in this process is actually working on it -- age plays no role.
    state = _running_state()  # started_at = 100.0, step = "fetch"

    result = expire_stale_running(state, now=100.5, runner_alive=False)

    assert result.job.state == "failed"
    assert result.job.step == "fetch"
    assert result.job.target == "v2.0.0"
    assert result.job.error is not None


def test_expire_stale_running_is_not_fooled_by_a_backwards_clock_step() -> None:
    # A Pi rebooting with no RTC can land `now` anywhere, including before
    # `started_at` -- expire_stale_running does no `now - started_at`
    # arithmetic at all, so this must behave identically to any other
    # `runner_alive=False` case.
    state = _running_state()  # started_at = 100.0

    result = expire_stale_running(state, now=0.0, runner_alive=False)

    assert result.job.state == "failed"


def test_expire_stale_running_uses_start_as_the_step_when_none_was_reached_yet() -> None:
    state = UpdateState(
        latest=RELEASE_V2,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="running", kind="update", target="v2.0.0", step=None, error=None, started_at=100.0, finished_at=None
        ),
    )

    result = expire_stale_running(state, now=1000.0, runner_alive=False)

    assert result.job.state == "failed"
    assert result.job.step == "start"


def test_current_update_state_persists_and_returns_the_expired_result_when_something_changed() -> None:
    store = FakeStateStore()
    store.write_update_state(_running_state())  # started_at = 100.0, step = "fetch"

    result = current_update_state(store, now=1000.0, runner_alive=False)

    assert result.job.state == "failed"
    assert store.read_update_state().job.state == "failed"


def test_current_update_state_does_not_write_when_nothing_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStateStore()
    store.write_update_state(_running_state())
    written_calls: list[UpdateState] = []
    original_write = store.write_update_state

    def spy_write(state: UpdateState) -> None:
        written_calls.append(state)
        original_write(state)

    monkeypatch.setattr(store, "write_update_state", spy_write)

    result = current_update_state(store, now=1000.0, runner_alive=True)

    assert result.job.state == "running"
    assert written_calls == []


def test_current_update_state_applies_expire_stale_restart_before_expire_stale_running() -> None:
    store = FakeStateStore()
    state = mark_restarting(_running_state(), now=200.0)  # started_at 100.0, restarting_at 200.0
    store.write_update_state(state)

    result = current_update_state(store, now=200.0 + DEFAULT_RESTART_MAX_AGE_SECONDS + 1, runner_alive=True)

    assert result.job.state == "failed"
    assert result.job.step == "restart"


def test_is_retry_available_is_true_when_the_failed_job_target_matches_latest() -> None:
    job = UpdateJob(
        state="failed", kind="update", target="v2.0.0", step="sync", error="boom", started_at=1.0, finished_at=2.0
    )

    assert is_retry_available(job, RELEASE_V2) is True


def test_is_retry_available_is_false_when_the_job_is_not_failed() -> None:
    job = UpdateJob(
        state="running", kind="update", target="v2.0.0", step="sync", error=None, started_at=1.0, finished_at=None
    )

    assert is_retry_available(job, RELEASE_V2) is False


def test_is_retry_available_is_false_when_the_target_does_not_match_latest() -> None:
    job = UpdateJob(
        state="failed", kind="update", target="v1.9.0", step="sync", error="boom", started_at=1.0, finished_at=2.0
    )

    assert is_retry_available(job, RELEASE_V2) is False


def test_is_retry_available_is_false_without_a_latest_release() -> None:
    job = UpdateJob(
        state="failed", kind="update", target="v2.0.0", step="sync", error="boom", started_at=1.0, finished_at=2.0
    )

    assert is_retry_available(job, None) is False
