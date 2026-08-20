"""Unit tests for password hashing, session tokens, and the login rate limiter."""

from __future__ import annotations

import threading

import pytest

from palmimo_portal.core.auth import (
    InvalidCurrentPasswordError,
    LoginRateLimiter,
    PasswordAlreadySetError,
    PasswordNotSetError,
    ResetDecision,
    ResetRateLimiter,
    change_password,
    change_password_from_full,
    change_password_from_initial,
    decide_reset,
    decode_session,
    generate_signing_key,
    hash_password,
    issue_session,
    setup_password,
    verify_password,
    verify_password_against_store,
    verify_session,
)
from palmimo_portal.core.identity import PortalAuthState
from palmimo_portal.ports import IDENTITY_UNAVAILABLE, AuthAlreadyExistsError, AuthLockTimeoutError, Identity
from palmimo_portal.testing.fakes import FakeStateStore


def test_hash_and_verify_password_round_trip() -> None:
    password_hash = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", password_hash) is True
    assert verify_password("wrong password", password_hash) is False


def test_setup_password_stores_hash_and_signing_key() -> None:
    store = FakeStateStore()

    state = setup_password(store, "hunter2")

    assert store.read_auth() == state
    assert verify_password("hunter2", state.password_hash)
    assert state.signing_key


def test_setup_password_raises_when_already_set() -> None:
    store = FakeStateStore()
    setup_password(store, "hunter2")

    with pytest.raises(PasswordAlreadySetError):
        setup_password(store, "different")


def test_change_password_rotates_the_signing_key() -> None:
    store = FakeStateStore()
    original = setup_password(store, "hunter2")

    updated = change_password(store, "new-password")

    assert updated.signing_key != original.signing_key
    assert verify_password("new-password", updated.password_hash)


def test_change_password_invalidates_sessions_signed_under_the_old_key() -> None:
    store = FakeStateStore()
    original = setup_password(store, "hunter2")
    token = issue_session(original.signing_key)
    assert verify_session(original.signing_key, token) is True

    updated = change_password(store, "new-password")

    assert verify_session(updated.signing_key, token) is False


def test_verify_password_against_store_raises_before_setup() -> None:
    store = FakeStateStore()

    with pytest.raises(PasswordNotSetError):
        verify_password_against_store(store, "anything")


def test_verify_password_against_store_reports_match() -> None:
    store = FakeStateStore()
    setup_password(store, "hunter2")

    assert verify_password_against_store(store, "hunter2") is True
    assert verify_password_against_store(store, "wrong") is False


def test_generate_signing_key_is_random() -> None:
    assert generate_signing_key() != generate_signing_key()


def test_session_token_rejects_tampering() -> None:
    signing_key = generate_signing_key()
    token = issue_session(signing_key)

    assert verify_session(signing_key, token) is True
    assert verify_session(signing_key, token + "x") is False
    assert verify_session(generate_signing_key(), token) is False


def test_session_token_expires() -> None:
    signing_key = generate_signing_key()
    token = issue_session(signing_key)

    # A negative max_age is always exceeded by a non-negative elapsed age,
    # so this is a deterministic "already expired" check with no sleeping.
    assert verify_session(signing_key, token, max_age=-1) is False


def test_issue_session_defaults_to_full_mode() -> None:
    signing_key = generate_signing_key()
    token = issue_session(signing_key)

    payload = decode_session(signing_key, token)

    assert payload is not None
    assert payload["mode"] == "full"


def test_issue_session_carries_the_given_mode() -> None:
    signing_key = generate_signing_key()
    token = issue_session(signing_key, mode="initial")

    payload = decode_session(signing_key, token)

    assert payload is not None
    assert payload["mode"] == "initial"


def test_decode_session_is_none_for_a_tampered_token() -> None:
    signing_key = generate_signing_key()
    token = issue_session(signing_key)

    assert decode_session(signing_key, token + "x") is None


IDENTITY = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker-password"))


def test_change_password_from_initial_creates_auth_material() -> None:
    store = FakeStateStore()

    state = change_password_from_initial(store, IDENTITY, "sticker-password", "new-password")

    assert store.read_auth() == state
    assert verify_password("new-password", state.password_hash)


def test_change_password_from_initial_rejects_the_wrong_current_password() -> None:
    store = FakeStateStore()

    with pytest.raises(InvalidCurrentPasswordError):
        change_password_from_initial(store, IDENTITY, "wrong-sticker-password", "new-password")

    assert store.read_auth() is None


def test_change_password_from_initial_race_the_loser_gets_auth_already_exists() -> None:
    # Two initial-mode sessions both submitting a correct current_password
    # -- create_auth's O_CREAT|O_EXCL semantics mean only one of them may
    # actually create auth.json.
    store = FakeStateStore()
    change_password_from_initial(store, IDENTITY, "sticker-password", "winner-password")

    with pytest.raises(AuthAlreadyExistsError):
        change_password_from_initial(store, IDENTITY, "sticker-password", "loser-password")

    winner_auth = store.read_auth()
    assert winner_auth is not None
    assert verify_password("winner-password", winner_auth.password_hash)


def test_change_password_from_full_rotates_the_hash_and_signing_key() -> None:
    store = FakeStateStore()
    original = setup_password(store, "hunter2")

    updated = change_password_from_full(store, "hunter2", "new-password")

    assert updated.signing_key != original.signing_key
    assert verify_password("new-password", updated.password_hash)


def test_change_password_from_full_rejects_the_wrong_current_password() -> None:
    store = FakeStateStore()
    original = setup_password(store, "hunter2")

    with pytest.raises(InvalidCurrentPasswordError):
        change_password_from_full(store, "wrong", "new-password")

    assert store.read_auth() == original


def test_change_password_from_full_two_sequential_calls_both_succeed() -> None:
    store = FakeStateStore()
    setup_password(store, "hunter2")

    first = change_password_from_full(store, "hunter2", "second-password")
    second = change_password_from_full(store, "second-password", "third-password")

    assert verify_password("third-password", second.password_hash)
    assert first.signing_key != second.signing_key


def test_change_password_from_full_serializes_concurrent_callers() -> None:
    # Two threads racing change_password_from_full against the same store
    # must not interleave one's read-verify-write with the other's --
    # store.lock_auth() (held across the whole verify-then-write sequence)
    # is what change_password_from_full must use to guarantee that. Proven
    # here by holding the lock from the main thread first and confirming a
    # concurrent call blocks until it is released, rather than proceeding
    # to read a hash that could change out from under it mid-call.
    store = FakeStateStore()
    setup_password(store, "hunter2")

    result: dict[str, object] = {}

    def worker() -> None:
        result["state"] = change_password_from_full(store, "hunter2", "new-password")

    with store.lock_auth():
        thread = threading.Thread(target=worker)
        thread.start()
        # The worker is blocked trying to acquire lock_auth() -- give it a
        # moment to prove it has NOT completed while the lock is held.
        thread.join(timeout=0.2)
        assert thread.is_alive(), "change_password_from_full must block while the lock is held"

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert "state" in result


def test_fake_state_store_lock_auth_raises_after_the_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # FakeStateStore.lock_auth mirrors JsonFileStateStore.lock_auth's bounded
    # wait (see palmimo_portal.testing.fakes's docstring), not an unbounded
    # threading.Lock -- a stuck contender must raise, not hang a test.
    monkeypatch.setattr("palmimo_portal.testing.fakes.AUTH_LOCK_TIMEOUT_SECONDS", 0.2)
    store = FakeStateStore()
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.lock_auth():
            holding.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=2.0)

    with pytest.raises(AuthLockTimeoutError), store.lock_auth():
        pass  # pragma: no cover -- must never be entered

    release.set()
    thread.join(timeout=5.0)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_locks_out_after_five_failures() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)

    for _ in range(4):
        limiter.record_failure()
        assert limiter.is_locked() is False

    limiter.record_failure()

    assert limiter.is_locked() is True
    assert limiter.seconds_remaining() == pytest.approx(60.0)


def test_rate_limiter_unlocks_after_the_lockout_expires() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.record_failure()
    assert limiter.is_locked() is True

    clock.now += 60.0

    assert limiter.is_locked() is False


def test_rate_limiter_logs_a_warning_once_when_lockout_engages(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        for _ in range(5):
            limiter.record_failure()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "lockout" in warnings[0].message
    assert "5" in warnings[0].message
    assert "60" in warnings[0].message


def test_rate_limiter_logs_a_fresh_warning_on_a_second_lockout(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.record_failure()
    clock.now += 60.0
    assert limiter.is_locked() is False  # lockout expired, failure count reset
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        for _ in range(5):
            limiter.record_failure()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_rate_limiter_success_clears_the_failure_count() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(4):
        limiter.record_failure()

    limiter.record_success()
    for _ in range(4):
        limiter.record_failure()

    assert limiter.is_locked() is False


def test_try_attempt_reserves_a_pending_slot_that_does_not_by_itself_lock_out() -> None:
    # Under the failures/pending split, try_attempt() alone (with nothing
    # ever resolved) never engages the lockout -- only record_failure()
    # does. Five outstanding reservations still fill the shared budget
    # (failures + pending >= max_failures), so a 6th is denied, but the
    # limiter is not "locked" in the is_locked()/seconds_remaining() sense.
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)

    for _ in range(5):
        assert limiter.try_attempt() is True
    assert limiter.is_locked() is False
    assert limiter.try_attempt() is False  # budget exhausted by pending, not by a lockout


def test_sequential_failures_via_try_attempt_lock_out_at_the_threshold() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)

    for _ in range(4):
        assert limiter.try_attempt() is True
        limiter.record_failure()
    assert limiter.is_locked() is False

    assert limiter.try_attempt() is True
    limiter.record_failure()  # the 5th confirmed failure trips the lockout

    assert limiter.is_locked() is True


def test_try_attempt_returns_false_once_locked_and_does_not_extend_the_lockout() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.try_attempt()
        limiter.record_failure()
    assert limiter.is_locked() is True
    locked_until_before = limiter.seconds_remaining()

    assert limiter.try_attempt() is False

    assert limiter.seconds_remaining() == pytest.approx(locked_until_before)


def test_record_success_releases_only_its_own_reservation() -> None:
    # record_success() (like record_failure()/release()) resolves exactly
    # one pending reservation -- the caller's own -- not every outstanding
    # one. Five concurrent reservations, one resolved correct: the budget
    # frees up by exactly one slot, not all five.
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        assert limiter.try_attempt() is True
    assert limiter.try_attempt() is False  # budget exhausted

    limiter.record_success()

    assert limiter.try_attempt() is True  # exactly one slot freed
    assert limiter.try_attempt() is False  # and no more than that


def test_try_attempt_reservation_is_released_by_release() -> None:
    # release() -- used for every early exit that never actually judged the
    # credential -- resolves a reservation as neither a failure nor a
    # success: no lockout progress, but the budget is freed.
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        assert limiter.try_attempt() is True
    assert limiter.try_attempt() is False

    limiter.release()

    assert limiter.try_attempt() is True
    assert limiter.is_locked() is False


def test_20_concurrent_wrong_password_attempts_against_a_locked_out_limiter_are_all_denied() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(5):
        limiter.try_attempt()
        limiter.record_failure()
    assert limiter.is_locked() is True

    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        allowed = limiter.try_attempt()
        with results_lock:
            results.append(allowed)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [False] * 20


def test_8_concurrent_fresh_attempts_at_most_five_reach_the_verify_step() -> None:
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    barrier = threading.Barrier(8)
    verify_step_count = 0
    count_lock = threading.Lock()

    def attempt() -> None:
        nonlocal verify_step_count
        barrier.wait()
        if limiter.try_attempt():
            with count_lock:
                verify_step_count += 1

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert verify_step_count <= 5


def test_5_concurrent_correct_logins_all_succeed_and_leave_failures_at_zero() -> None:
    # The regression this whole failures/pending split exists to fix: a
    # burst of *correct* concurrent logins must not accidentally burn the
    # failure budget just because each one takes a moment to verify.
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    reserved: list[bool] = []
    reserved_lock = threading.Lock()
    barrier = threading.Barrier(5)

    def attempt() -> None:
        barrier.wait()
        allowed = limiter.try_attempt()
        with reserved_lock:
            reserved.append(allowed)
        if allowed:
            limiter.record_success()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert reserved == [True] * 5
    assert limiter.is_locked() is False


def test_8_concurrent_wrong_logins_all_resolve_and_the_budget_ends_up_locked() -> None:
    # Every one of the 8 concurrent attempts gets a definitive answer (no
    # hang, no leaked reservation): at most 5 win a reservation (the shared
    # budget), the rest are denied outright, and once the 5 winners each
    # resolve their reservation as a confirmed failure, the limiter locks
    # out -- proving pending reservations and confirmed failures share the
    # same cap end-to-end, not just at the try_attempt() step.
    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        allowed = limiter.try_attempt()
        with results_lock:
            results.append(allowed)
        if allowed:
            limiter.record_failure()

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 5
    assert results.count(False) == 3
    assert limiter.is_locked() is True


def test_sequential_four_wrong_then_one_correct_login_never_logs_the_lockout_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    clock = _FakeClock()
    limiter = LoginRateLimiter(clock=clock)

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        for _ in range(4):
            assert limiter.try_attempt() is True
            limiter.record_failure()
        assert limiter.try_attempt() is True
        limiter.record_success()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == []
    assert limiter.is_locked() is False


def test_reset_rate_limiter_is_not_locked_before_any_reset() -> None:
    limiter = ResetRateLimiter(clock=_FakeClock())

    assert limiter.is_locked() is False
    assert limiter.seconds_remaining() == 0.0


def test_reset_rate_limiter_try_acquire_succeeds_and_locks_immediately() -> None:
    limiter = ResetRateLimiter(clock=_FakeClock())

    acquired = limiter.try_acquire()

    assert acquired is True
    assert limiter.is_locked() is True
    assert limiter.seconds_remaining() == pytest.approx(60.0)


def test_reset_rate_limiter_unlocks_after_the_window_expires() -> None:
    clock = _FakeClock()
    limiter = ResetRateLimiter(clock=clock)
    limiter.try_acquire()
    assert limiter.is_locked() is True

    clock.now += 60.0

    assert limiter.is_locked() is False
    assert limiter.seconds_remaining() == 0.0


def test_reset_rate_limiter_try_acquire_fails_while_already_locked() -> None:
    clock = _FakeClock()
    limiter = ResetRateLimiter(clock=clock)
    assert limiter.try_acquire() is True

    second = limiter.try_acquire()

    # A denied acquisition must leave the existing window untouched, not
    # extend it -- otherwise a flood of doomed concurrent requests could
    # keep pushing the window out indefinitely.
    assert second is False
    assert limiter.seconds_remaining() == pytest.approx(60.0)


def test_reset_rate_limiter_try_acquire_two_concurrent_callers_exactly_one_wins() -> None:
    # The atomicity guarantee try_acquire exists for: two threads racing
    # the same limiter must not both observe "not locked" and both proceed
    # -- exactly one may win, protected by the limiter's internal lock.
    limiter = ResetRateLimiter(clock=_FakeClock())
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait(timeout=5.0)
        results.append(limiter.try_acquire())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert sorted(results) == [False, True]
    assert limiter.is_locked() is True


def test_reset_rate_limiter_release_reopens_the_window() -> None:
    # For a caller that acquired the budget but then failed to actually
    # perform the reset (e.g. StateStore.delete_auth() raised) -- the
    # acquisition must not be permanently wasted.
    limiter = ResetRateLimiter(clock=_FakeClock())
    limiter.try_acquire()
    assert limiter.is_locked() is True

    limiter.release()

    assert limiter.is_locked() is False
    assert limiter.seconds_remaining() == 0.0
    assert limiter.try_acquire() is True


STICKER_IDENTITY = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker"))


@pytest.mark.parametrize(
    ("auth_state", "identity", "expected"),
    [
        (PortalAuthState.SET, STICKER_IDENTITY, ResetDecision.ALLOW),
        (PortalAuthState.CORRUPT, STICKER_IDENTITY, ResetDecision.ALLOW),
        (PortalAuthState.OPEN_SETUP, None, ResetDecision.DENY_NOT_AVAILABLE),
        # A DIY device that has since completed /auth/setup also reaches
        # SET/CORRUPT with no identity file -- must still be refused (see
        # decide_reset's docstring for why auth_state alone is not enough).
        (PortalAuthState.SET, None, ResetDecision.DENY_NOT_AVAILABLE),
        (PortalAuthState.CORRUPT, None, ResetDecision.DENY_NOT_AVAILABLE),
        (PortalAuthState.INITIAL, STICKER_IDENTITY, ResetDecision.DENY_ALREADY_INITIAL),
        (PortalAuthState.UNAVAILABLE, IDENTITY_UNAVAILABLE, ResetDecision.DENY_UNAVAILABLE),
        # An unavailable identity read while auth.json is already SET/CORRUPT
        # never surfaces as PortalAuthState.UNAVAILABLE (compute_auth_state
        # only consults identity when auth.json is ABSENT) -- decide_reset
        # must still refuse rather than guess which kind of device this is.
        (PortalAuthState.SET, IDENTITY_UNAVAILABLE, ResetDecision.DENY_UNAVAILABLE),
        (PortalAuthState.CORRUPT, IDENTITY_UNAVAILABLE, ResetDecision.DENY_UNAVAILABLE),
    ],
)
def test_decide_reset_table(auth_state: PortalAuthState, identity: Identity | None, expected: ResetDecision) -> None:
    assert decide_reset(auth_state, identity) == expected


def test_fake_state_store_delete_auth_returns_to_absent() -> None:
    from palmimo_portal.ports import AuthFileState

    store = FakeStateStore()
    setup_password(store, "hunter2")

    store.delete_auth()

    assert store.auth_state() is AuthFileState.ABSENT
    assert store.read_auth() is None


def test_fake_state_store_delete_auth_is_a_no_op_when_absent() -> None:
    from palmimo_portal.ports import AuthFileState

    store = FakeStateStore()

    store.delete_auth()  # must not raise

    assert store.auth_state() is AuthFileState.ABSENT


def test_fake_state_store_delete_auth_clears_a_corrupt_flag_too() -> None:
    from palmimo_portal.ports import AuthFileState

    store = FakeStateStore(auth_corrupt=True)

    store.delete_auth()

    assert store.auth_state() is AuthFileState.ABSENT


def test_fake_state_store_delete_auth_rotates_an_existing_initial_signing_key() -> None:
    store = FakeStateStore()
    old_key = store.read_or_create_initial_signing_key()
    setup_password(store, "hunter2")

    store.delete_auth()

    new_key = store.read_or_create_initial_signing_key()
    assert new_key != old_key
