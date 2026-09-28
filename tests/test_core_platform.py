"""Tests for the platform-bundle update pipeline (core/platform.py, core/platform_update.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.core.platform import (
    PlatformLatestCache,
    PlatformUpdateRunner,
    compute_status,
    portal_satisfies_requirement,
)
from palmimo_portal.core.platform_update import IDLE_PLATFORM_STATE, finalize_after_restart
from palmimo_portal.ports import (
    AdapterUnavailableError,
    PlatformInstalled,
    PlatformJob,
    PlatformUpdateState,
    PlatformVerifyDiff,
    Release,
    ReleaseSourceError,
)
from palmimo_portal.testing.fakes import (
    FakePlatformBundleSource,
    FakePlatformPort,
    FakeReleaseSource,
    FakeStateStore,
    FakeSystemPort,
)


READY_MANIFEST = {
    "version": 2,
    "requires_portal": "0.0.0",
    "restart_portal": False,
    "summary": "adds camera device class",
    "reflash_required": False,
}


@pytest.mark.parametrize(
    ("installed", "required", "expected"),
    [
        ("0.2.0rc1", "0.2.0", False),
        ("0.2.0", "0.2.0rc1", True),
        ("v0.3.0", "0.2.9", True),
    ],
)
def test_portal_satisfies_requirement_uses_pep440_ordering(installed: str, required: str, expected: bool) -> None:
    assert portal_satisfies_requirement(installed, required) is expected


@pytest.fixture
def state_store() -> FakeStateStore:
    return FakeStateStore()


@pytest.fixture
def platform_port() -> FakePlatformPort:
    return FakePlatformPort(installed=None)


@pytest.fixture
def bundle_source() -> FakePlatformBundleSource:
    return FakePlatformBundleSource(manifest=dict(READY_MANIFEST), sha="a" * 64)


@pytest.fixture
def system() -> FakeSystemPort:
    return FakeSystemPort()


def _runner(
    state_store: FakeStateStore,
    bundle_source: FakePlatformBundleSource,
    platform_port: FakePlatformPort,
    system: FakeSystemPort,
    platform_dir: Path,
    *,
    portal_installed_version: str = "0.5.0",
) -> PlatformUpdateRunner:
    return PlatformUpdateRunner(
        state_store,
        bundle_source,
        platform_port,
        system,
        platform_dir,
        portal_installed_version=portal_installed_version,
    )


class TestReadiness:
    def test_not_ready_when_no_bundle_is_installed(self) -> None:
        status = compute_status(None, 1, [], None, None, None)

        assert status["ready"] is False
        assert status["reason"] == "platform_not_installed"

    def test_not_ready_when_installed_version_is_below_required(self) -> None:
        installed = PlatformInstalled(version=1, installed_at="2026-01-01T00:00:00Z", bundle_sha256="a" * 64)

        status = compute_status(installed, 2, [], None, None, None)

        assert status["ready"] is False
        assert status["reason"] == "platform_outdated"

    def test_not_ready_when_verify_reports_a_diff(self) -> None:
        installed = PlatformInstalled(version=2, installed_at="2026-01-01T00:00:00Z", bundle_sha256="a" * 64)
        diffs = [PlatformVerifyDiff(path="/usr/lib/palmimo/app-launch", kind="missing")]

        status = compute_status(installed, 2, diffs, None, None, None)

        assert status["ready"] is False
        assert status["reason"] == "platform_verify_failed"

    def test_ready_when_installed_version_meets_required_and_verify_is_clean(self) -> None:
        installed = PlatformInstalled(version=2, installed_at="2026-01-01T00:00:00Z", bundle_sha256="a" * 64)

        status = compute_status(installed, 2, [], None, None, None)

        assert status["ready"] is True
        assert status["reason"] is None

    def test_not_ready_when_verify_has_never_run(self) -> None:
        # A bundle that has never actually been checked (no cached bundle yet, or the last verify
        # command itself crashed/timed out) must not be reported the same as "checked and clean".
        installed = PlatformInstalled(version=2, installed_at="2026-01-01T00:00:00Z", bundle_sha256="a" * 64)

        status = compute_status(installed, 2, None, None, None, None)

        assert status["ready"] is False
        assert status["reason"] == "platform_verify_unavailable"


class TestUpdateJobPipeline:
    def test_successful_job_installs_verifies_and_records_in_order(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # Without step ordering enforced here, a bug that ran `record` before `install`
        # (writing installed.json for a bundle never actually applied) would go unnoticed.
        platform_port.next_version = 2
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        assert len(platform_port.install_calls) == 1
        assert len(platform_port.verify_calls) == 1
        assert len(platform_port.record_calls) == 1
        assert platform_port.install_calls[0] == platform_port.verify_calls[0] == platform_port.record_calls[0][0]
        job = state_store.read_platform_update_state().job
        assert job.state == "done"

    def test_successful_job_does_not_write_to_the_current_bundle_cache(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # install.sh install already populates platform_dir/"current" as root, with its own
        # ownership/permissions (design doc 2.8) -- the Portal process replacing it afterwards
        # would race that copy and leave files it does not own.
        platform_port.next_version = 2
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        assert not (tmp_path / "current").exists()

    def test_sha_mismatch_aborts_before_any_sudo_backed_call(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # The whole point of verifying the tarball's checksum before touching install.sh:
        # a corrupted/tampered download must never reach a sudo-backed command.
        bundle_source.fail_on = "verify_sha"
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        assert platform_port.install_calls == []
        assert platform_port.verify_calls == []
        assert platform_port.record_calls == []
        job = state_store.read_platform_update_state().job
        assert job.state == "failed"
        assert job.step == "verify_sha"

    @pytest.mark.parametrize(
        ("manifest_overrides", "expected_step"),
        [
            ({"reflash_required": True}, "preflight"),
            ({"requires_portal": "99.0.0"}, "preflight"),
        ],
    )
    def test_preflight_refusal_fails_before_install(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
        manifest_overrides: dict[str, object],
        expected_step: str,
    ) -> None:
        bundle_source.manifest = {**READY_MANIFEST, **manifest_overrides}
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path, portal_installed_version="0.5.0")

        runner.run("v2", target_version=2)

        assert platform_port.install_calls == []
        job = state_store.read_platform_update_state().job
        assert job.state == "failed"
        assert job.step == expected_step

    def test_verify_diffs_after_install_fail_the_job_without_recording(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # A diff after install means the bundle did not actually converge --
        # recording it anyway would make installed.json lie about the device's real state.
        platform_port.verify_diffs = [
            PlatformVerifyDiff(path="/etc/polkit-1/rules.d/50-x.rules", kind="mode", expected="0644", actual="0640")
        ]
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        assert len(platform_port.install_calls) == 1
        assert platform_port.record_calls == []
        job = state_store.read_platform_update_state().job
        assert job.state == "failed"
        assert job.step == "verify"

    @pytest.mark.parametrize(("restart_portal", "expected_calls"), [(True, 1), (False, 0)])
    def test_restart_only_happens_when_the_manifest_asks_for_it(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
        restart_portal: bool,
        expected_calls: int,
    ) -> None:
        bundle_source.manifest = {**READY_MANIFEST, "restart_portal": restart_portal}
        platform_port.next_version = 2
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        assert system.restart_calls == expected_calls

    def test_a_restart_portal_failure_leaves_the_job_done_not_failed(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # The job's success is decided entirely by record, before restart_portal is ever
        # called -- a device that cannot restart itself must not retroactively lose the fact
        # that install/verify/record already succeeded.
        bundle_source.manifest = {**READY_MANIFEST, "restart_portal": True}
        platform_port.next_version = 2
        system.raise_on_restart_portal = AdapterUnavailableError("system_backend_unavailable", "dbus timeout")
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)

        job = state_store.read_platform_update_state().job
        assert job.state == "done"

    def test_restart_happens_after_on_finish_not_before(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        # on_finish refreshes the cached readiness snapshot this (still-running) process serves --
        # it must run before restart_portal can end the process, not after.
        bundle_source.manifest = {**READY_MANIFEST, "restart_portal": True}
        platform_port.next_version = 2
        call_order: list[str] = []
        original_restart = system.restart_portal

        def tracked_restart() -> None:
            call_order.append("restart")
            original_restart()

        system.restart_portal = tracked_restart  # type: ignore[method-assign]
        runner = PlatformUpdateRunner(
            state_store,
            bundle_source,
            platform_port,
            system,
            tmp_path,
            portal_installed_version="0.5.0",
            on_finish=lambda: call_order.append("on_finish"),
        )

        runner.run("v2", target_version=2)

        assert call_order == ["on_finish", "restart"]


class TestJobCrashRecovery:
    def test_unexpected_exception_mid_step_marks_the_job_failed_with_the_step(
        self,
        tmp_path: Path,
        state_store: FakeStateStore,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        def _boom(bundle_dir: Path) -> None:
            raise RuntimeError("disk exploded")

        platform_port.install = _boom  # type: ignore[method-assign]
        runner = _runner(state_store, bundle_source, platform_port, system, tmp_path)

        runner.run("v2", target_version=2)  # must not raise out of the background thread

        job = state_store.read_platform_update_state().job
        assert job.state == "failed"
        assert job.step == "install"
        assert job.error == "unexpected: RuntimeError"

    def test_a_crashed_job_releases_platform_lock_so_the_next_update_can_start(
        self,
        tmp_path: Path,
        bundle_source: FakePlatformBundleSource,
        platform_port: FakePlatformPort,
        system: FakeSystemPort,
    ) -> None:
        from palmimo_portal.core.platform import PlatformJobRunner

        def _boom(bundle_dir: Path) -> None:
            raise RuntimeError("disk exploded")

        platform_port.install = _boom  # type: ignore[method-assign]
        state = FakeStateStore()
        updater = _runner(state, bundle_source, platform_port, system, tmp_path)
        job_runner = PlatformJobRunner(state, updater, run_in_thread=False)
        job_runner.start("v2", target_version=2)

        # Would raise PlatformLockTimeoutError if the crash above ever left platform.lock held.
        job_runner.start("v2", target_version=3)

        assert state.read_platform_update_state().job.target_version == 3


class TestStartupFinalize:
    def test_a_running_job_left_over_from_a_crash_is_marked_failed_interrupted(self) -> None:
        running = PlatformUpdateState(
            job=PlatformJob(
                state="running", target_version=3, step="install", error=None, started_at=1.0, finished_at=None
            )
        )

        finalized = finalize_after_restart(running, now=2.0)

        assert finalized.job.state == "failed"
        assert finalized.job.step == "install"
        assert finalized.job.error is not None

    @pytest.mark.parametrize("state", ["idle", "done", "failed"])
    def test_a_job_not_running_is_left_untouched(self, state: str) -> None:
        job = PlatformJob(state=state, target_version=None, step=None, error=None, started_at=None, finished_at=None)  # type: ignore[arg-type]

        finalized = finalize_after_restart(PlatformUpdateState(job=job), now=2.0)

        assert finalized.job.state == state

    def test_idle_state_constant_is_untouched(self) -> None:
        assert finalize_after_restart(IDLE_PLATFORM_STATE, now=2.0) is IDLE_PLATFORM_STATE


_LATEST_RELEASE = Release(tag="v2", name="v2", published_at="2026-01-01T00:00:00Z", html_url="https://example.test/v2")


class TestLatestCache:
    def test_get_prefers_the_manifest_asset_and_never_fetches_the_tarball(self, tmp_path: Path) -> None:
        bundle_source = FakePlatformBundleSource(manifest_asset=dict(READY_MANIFEST, version=3))
        cache = PlatformLatestCache(
            FakeReleaseSource(latest=_LATEST_RELEASE), bundle_source, FakeStateStore(), tmp_path
        )

        manifest, tag, error = cache.get(ntp_synchronized=True)

        assert manifest is not None and manifest.version == 3
        assert tag == "v2"
        assert error is None
        assert bundle_source.fetch_calls == []

    def test_get_falls_back_to_the_tarball_when_no_manifest_asset_is_published(self, tmp_path: Path) -> None:
        # manifest_asset defaults to None -- simulates an older palmimo-image release.
        bundle_source = FakePlatformBundleSource(manifest=dict(READY_MANIFEST, version=3))
        cache = PlatformLatestCache(
            FakeReleaseSource(latest=_LATEST_RELEASE), bundle_source, FakeStateStore(), tmp_path
        )

        manifest, _tag, _error = cache.get(ntp_synchronized=True)

        assert manifest is not None and manifest.version == 3
        assert bundle_source.fetch_calls == ["v2"]

    def test_get_skips_refresh_and_keeps_the_previous_answer_while_a_job_holds_platform_lock(
        self, tmp_path: Path
    ) -> None:
        state = FakeStateStore()
        release_source = FakeReleaseSource(latest=_LATEST_RELEASE)
        bundle_source = FakePlatformBundleSource(manifest_asset=dict(READY_MANIFEST, version=3))
        cache = PlatformLatestCache(release_source, bundle_source, state, tmp_path)
        cache.get(ntp_synchronized=True)  # warm the cache with a real answer first

        lock_cm = state.lock_platform()
        lock_cm.__enter__()
        try:
            manifest, tag, _error = cache.get(ntp_synchronized=True, force=True)
        finally:
            lock_cm.__exit__(None, None, None)

        # An update job's own staging directory is exclusive to it (design doc 2.8) -- the
        # latest check must not fetch anything (tarball or manifest) while the lock is held.
        assert manifest is not None and manifest.version == 3
        assert tag == "v2"
        assert release_source.fetch_calls == 1
        assert bundle_source.fetch_manifest_calls == ["v2"]
        assert bundle_source.fetch_calls == []

    def test_get_reports_clock_unsynced_without_attempting_a_fetch(self, tmp_path: Path) -> None:
        release_source = FakeReleaseSource(latest=_LATEST_RELEASE)
        cache = PlatformLatestCache(release_source, FakePlatformBundleSource(), FakeStateStore(), tmp_path)

        manifest, _tag, error = cache.get(ntp_synchronized=False)

        assert release_source.fetch_calls == 0
        assert manifest is None
        assert error == "clock_unsynced"

    def test_get_reports_rate_limited_reason_from_a_rate_limited_release_source(self, tmp_path: Path) -> None:
        release_source = FakeReleaseSource(raise_on_fetch=ReleaseSourceError("rate_limited", "GitHub rate limit hit"))
        cache = PlatformLatestCache(release_source, FakePlatformBundleSource(), FakeStateStore(), tmp_path)

        _manifest, _tag, error = cache.get(ntp_synchronized=True)

        assert error == "rate_limited"

    def test_get_backs_off_after_a_failed_fetch_then_retries_once_the_backoff_elapses(self, tmp_path: Path) -> None:
        release_source = FakeReleaseSource(raise_on_fetch=ReleaseSourceError("release_source_unavailable", "down"))
        clock = [0.0]
        cache = PlatformLatestCache(
            release_source, FakePlatformBundleSource(), FakeStateStore(), tmp_path, now=lambda: clock[0]
        )

        cache.get(ntp_synchronized=True)
        clock[0] = 100.0  # inside the 300s backoff window
        cache.get(ntp_synchronized=True)
        assert release_source.fetch_calls == 1

        clock[0] = 301.0  # past the backoff window
        cache.get(ntp_synchronized=True)
        assert release_source.fetch_calls == 2

    def test_check_now_bypasses_the_ttl_and_returns_fresh_data_when_the_cache_is_stale(self, tmp_path: Path) -> None:
        release_source = FakeReleaseSource(latest=_LATEST_RELEASE)
        bundle_source = FakePlatformBundleSource(manifest_asset=dict(READY_MANIFEST, version=3))
        clock = [0.0]
        cache = PlatformLatestCache(release_source, bundle_source, FakeStateStore(), tmp_path, now=lambda: clock[0])
        cache.get(ntp_synchronized=True)  # warm the cache, inside the 1h TTL

        clock[0] = 120.0  # past the 60s check rate limit, well inside the 1h TTL
        bundle_source.manifest_asset = dict(READY_MANIFEST, version=4)  # a new release was published between calls
        stale_manifest, _tag, _error = cache.get(ntp_synchronized=True)
        fresh_manifest, _tag, _error = cache.check_now(ntp_synchronized=True)

        assert stale_manifest is not None and stale_manifest.version == 3
        assert fresh_manifest is not None and fresh_manifest.version == 4
