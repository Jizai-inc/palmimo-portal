"""Tests for :mod:`palmimo_portal.core.update_runner`."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from palmimo_portal.core.update import IDLE_UPDATE_STATE, start_apply
from palmimo_portal.core.update_runner import UpdateRunner
from palmimo_portal.ports import AdapterUnavailableError, InstalledVersion, Release, UpdateState
from palmimo_portal.testing.fakes import FakeStateStore, FakeSystemPort, FakeUpdater


RELEASE_V2 = Release(
    tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v2"
)


def _running_state() -> UpdateState:
    state = UpdateState(latest=RELEASE_V2, checked_at=1000.0, previous_tag=None, job=IDLE_UPDATE_STATE.job)
    return start_apply(state, InstalledVersion(tag="v1.0.0", commit="abc"), "v2.0.0", now=1001.0)


def test_run_sleeps_restart_delay_seconds_before_restarting(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, restart_delay_seconds=2.5)
    runner.start("v2.0.0")

    assert sleep_calls == [2.5]
    assert system.restart_calls == 1


def test_run_defaults_restart_delay_to_one_second(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False)
    runner.start("v2.0.0")

    assert sleep_calls == [1.0]


def test_run_with_zero_delay_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, restart_delay_seconds=0.0)
    runner.start("v2.0.0")

    assert sleep_calls == [0.0]
    assert system.restart_calls == 1


def test_run_persists_mark_failed_when_a_non_step_exception_occurs(monkeypatch: pytest.MonkeyPatch) -> None:
    # An exception that is not UpdateStepError/AdapterUnavailableError --
    # e.g. an OSError from a full disk raised out of on_step's own
    # write_update_state call -- must not leave the job wedged in
    # "running" forever.
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()

    original_write = state_store.write_update_state
    call_count = 0

    def flaky_write(state: UpdateState) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OSError("no space left on device")
        original_write(state)

    monkeypatch.setattr(state_store, "write_update_state", flaky_write)

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False)
    runner.start("v2.0.0")

    final_job = state_store.read_update_state().job
    assert final_job.state == "failed"
    # The write that would have recorded step="fetch" is the one that
    # raised, so no step was ever actually persisted -- falls back to
    # "start", the same convention finalize_after_restart uses.
    assert final_job.step == "start"
    assert "unexpected error" in (final_job.error or "")
    assert "no space left on device" in (final_job.error or "")
    assert system.restart_calls == 0


def test_run_leaves_a_diagnosable_state_when_every_write_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When even the failure-recording write itself fails (e.g. the same full disk that caused
    the original error), the runner must not silently swallow it -- log it with a traceback so
    an operator can see the job is stuck "running" and why.
    """
    import logging

    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()

    def always_fails(state: UpdateState) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(state_store, "write_update_state", always_fails)

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False)
    with caplog.at_level(logging.ERROR):
        runner.start("v2.0.0")  # must not raise out of start()

    assert "failed to persist the failure state" in caplog.text
    assert system.restart_calls == 0


def test_run_attributes_a_non_adapter_unavailable_restart_exception_to_the_restart_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bug in a SystemPort implementation (or any exception other than
    # AdapterUnavailableError) raised from restart_portal() must not be
    # misattributed to whatever step advance() last recorded (e.g.
    # "install-assets", the step before mark_restarting overwrote
    # job.state without touching job.step).
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    system.raise_on_restart_portal = RuntimeError("boom")
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, restart_delay_seconds=0.0)
    runner.start("v2.0.0")

    final_job = state_store.read_update_state().job
    assert final_job.state == "failed"
    assert final_job.step == "restart"
    assert "boom" in (final_job.error or "")


def test_run_sleeps_before_restart_even_when_restart_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    system.raise_on_restart_portal = AdapterUnavailableError("system_backend_unavailable", "logind down")
    sleep_calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleep_calls.append(seconds))

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, restart_delay_seconds=1.0)
    runner.start("v2.0.0")

    assert sleep_calls == [1.0]
    assert state_store.read_update_state().job.state == "failed"


# -- alive liveness flag ------------------------------------------------------------------


def test_run_sets_alive_for_the_whole_duration_of_a_synchronous_run(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    alive = threading.Event()
    observed_during_run: list[bool] = []

    original_apply = updater.apply

    def spying_apply(tag: str, on_step: Callable[[str], None]) -> None:
        observed_during_run.append(alive.is_set())
        original_apply(tag, on_step)

    monkeypatch.setattr(updater, "apply", spying_apply)

    assert alive.is_set() is False
    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, restart_delay_seconds=0.0, alive=alive)
    runner.start("v2.0.0")

    assert observed_during_run == [True]  # set for the duration of the run
    assert alive.is_set() is False  # cleared once the run finishes


def test_run_clears_alive_even_when_the_job_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    updater.fail_at_step = "sync"
    system = FakeSystemPort()
    alive = threading.Event()

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, alive=alive)
    runner.start("v2.0.0")

    assert alive.is_set() is False


def test_run_clears_alive_even_when_every_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # The `finally` clearing alive must run even down the "everything about
    # this job's persistence failed" path -- otherwise a runner killed by a
    # full disk would leave the liveness flag stuck True forever, which
    # would make expire_stale_running never unstick the job either.
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    updater = FakeUpdater()
    system = FakeSystemPort()

    def always_fails(state: UpdateState) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(state_store, "write_update_state", always_fails)
    alive = threading.Event()

    runner = UpdateRunner(state_store, updater, system, run_in_thread=False, alive=alive)
    runner.start("v2.0.0")

    assert alive.is_set() is False


def test_run_in_a_background_thread_keeps_alive_set_until_the_thread_finishes() -> None:
    # Drives a real background thread (run_in_thread=True) with an Updater
    # whose apply() blocks on an Event, so the test can observe `alive` is
    # True mid-run and False once the thread actually completes -- proving
    # the liveness flag reflects a real running thread, not just something
    # the synchronous code path happens to toggle.
    state_store = FakeStateStore()
    state_store.write_update_state(_running_state())
    system = FakeSystemPort()
    alive = threading.Event()
    apply_started = threading.Event()
    release_apply = threading.Event()

    class BlockingUpdater:
        def installed(self) -> InstalledVersion:
            return InstalledVersion(tag="v1.0.0", commit="abc")

        def apply(self, tag: str, on_step: Callable[[str], None]) -> None:
            apply_started.set()
            release_apply.wait(timeout=5.0)
            on_step("fetch")

    runner = UpdateRunner(
        state_store, BlockingUpdater(), system, run_in_thread=True, restart_delay_seconds=0.0, alive=alive
    )

    assert alive.is_set() is False
    runner.start("v2.0.0")
    assert apply_started.wait(timeout=5.0)
    assert alive.is_set() is True  # mid-run, on a real background thread

    release_apply.set()
    # Give the background thread a moment to finish and clear the flag.
    for _ in range(100):
        if not alive.is_set():
            break
        time.sleep(0.05)
    assert alive.is_set() is False
