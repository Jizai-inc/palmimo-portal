"""Tests for :mod:`palmimo_portal.adapters.git_uv_updater`.

A fake ``runner`` stands in for subprocess.run (the ``fetch``/``checkout``/
``sync`` steps); a fake ``opener`` stands in for ``urllib.request.urlopen``
(the ``assets`` step) -- see ``tests/test_github_releases_adapter.py`` for
the sibling adapter this opener-injection shape is borrowed from.

``apply``'s step order is ``fetch -> assets -> checkout -> sync ->
install-assets`` -- see the adapter module's docstring for the rationale
(the frontend asset is downloaded, verified, and staged *before* the tree is
touched, and only swapped into ``static/`` once ``checkout``/``sync`` have
both succeeded). Tests below that need an asset download to succeed even
though they are only exercising ``checkout``/``sync`` behavior always supply
an opener -- with the new order, ``assets`` runs unconditionally before
either.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.git_uv_updater import (
    _CHECKOUT_MARKER_NAME,
    _STALE_LOCK_MIN_AGE_SECONDS,
    DIRTY_TREE_REFUSAL_PREFIX,
    GitUvUpdater,
)
from palmimo_portal.adapters.static_asset import ASSET_MAX_BYTES, MEMBER_MAX_BYTES
from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE
from palmimo_portal.ports import InstalledVersion, UpdateStepError


class _ScriptedRunner:
    """Records every call and returns/raises whatever ``script`` says for that argv[1] (the git/uv subcommand)."""

    def __init__(self, script: dict[str, Any]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": argv, **kwargs})
        key = argv[1]
        outcome = self._script.get(key, subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
        if callable(outcome) and not isinstance(outcome, subprocess.CompletedProcess):
            outcome = outcome()  # a _seq(...) value -- the next scripted outcome for this key
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


def _build_tar(members: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> bytes:
    """Build an in-memory ``.tar.gz`` -- ``{member_path: content}`` for plain files, plus any raw ``TarInfo``s."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for info in extra or []:
            tar.addfile(info)
    return buffer.getvalue()


class _FakeAssetResponse:
    """Stand-in for ``urlopen``'s context-manager response, capped like the real read the adapter does."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int | None = None) -> bytes:
        return self._body if size is None else self._body[:size]

    def __enter__(self) -> _FakeAssetResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _asset_opener(tag: str, tar_bytes: bytes, *, sha256_hex: str | None = None) -> Any:
    """A scripted opener resolving both the ``assets`` step's downloads for one tag: the tarball and its sha256."""
    asset_name = STATIC_ASSET_NAME_TEMPLATE.format(tag=tag)
    digest = sha256_hex if sha256_hex is not None else hashlib.sha256(tar_bytes).hexdigest()
    sha_body = f"{digest}  {asset_name}\n".encode()

    def opener(request: urllib.request.Request, timeout: float) -> _FakeAssetResponse:
        if request.full_url.endswith(".sha256"):
            return _FakeAssetResponse(sha_body)
        return _FakeAssetResponse(tar_bytes)

    return opener


def _minimal_static_tar() -> bytes:
    return _build_tar({"static/index.html": b"<html></html>", "static/assets/index.js": b"console.log(1)"})


def _static_dir(portal_dir: Path) -> Path:
    return portal_dir / "palmimo_portal" / "static"


def _seq(*outcomes: Any) -> Callable[[], Any]:
    """A script-dict value that returns successive ``outcomes`` on successive calls to the same subcommand key.

    ``_ScriptedRunner`` calls a callable script value with no arguments to
    get the outcome for *this* invocation -- used where a subcommand (e.g.
    ``status``, checked both before and after the mutating ``git checkout``
    call) must answer differently the second time it's called within one
    ``_checkout`` attempt.
    """
    iterator: Iterator[Any] = iter(outcomes)
    return lambda: next(iterator)


def _seed_git_dir(tmp_path: Path) -> Path:
    """Create a real ``.git/`` directory (with ``refs/heads`` and ``refs/tags``) so the marker/lock-sweep code activates.

    Most tests use a bare ``tmp_path`` with a scripted (non-real) ``git``
    runner -- ``_git_dir()`` then finds no ``.git`` directory and the
    marker/sweep mechanisms are (correctly) no-ops. Tests that exercise
    those mechanisms need a real ``.git/`` on disk, even though the git
    *commands* themselves stay scripted.
    """
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "tags").mkdir(parents=True)
    return git_dir


def _run_real_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} in {cwd} failed: {result.stderr}"
    return result


def _init_real_release_repo(base: Path, *, tracked_file: str = "app.py", v1_content: str = "v1\n") -> Path:
    """Build a REAL git repo (not scripted) with an ``origin`` remote and a ``v1.0.0`` tag, checked out detached.

    Used by the integration-style tests below that must exercise the
    marker mechanism against *real* git failure text and behavior (an
    untracked-file conflict, a genuine forced checkout) rather than a
    scripted stand-in -- the earlier, broken attribution designs looked
    correct against scripted runners and only fell apart against real git
    semantics. Returns the ``portal_dir`` (a clone of a freshly created
    ``origin`` sibling directory), checked out detached at ``v1.0.0``.
    """
    origin = base / "origin"
    origin.mkdir()
    _run_real_git(origin, "init", "-q")
    _run_real_git(origin, "config", "user.email", "test@example.com")
    _run_real_git(origin, "config", "user.name", "Test")
    (origin / tracked_file).write_text(v1_content)
    _run_real_git(origin, "add", tracked_file)
    _run_real_git(origin, "commit", "-q", "-m", "v1")
    _run_real_git(origin, "tag", "v1.0.0")

    portal_dir = base / "portal"
    _run_real_git(base, "clone", "-q", str(origin), str(portal_dir))
    _run_real_git(portal_dir, "checkout", "-q", "--detach", "v1.0.0")
    return portal_dir


def _publish_real_tag(origin: Path, tag: str, *, file_name: str, content: str) -> None:
    """Add a new tracked file with ``content`` to ``origin`` and tag the resulting commit ``tag``."""
    (origin / file_name).write_text(content)
    _run_real_git(origin, "add", file_name)
    _run_real_git(origin, "commit", "-q", "-m", tag)
    _run_real_git(origin, "tag", tag)


class _FailingStepRunner:
    """Wraps real ``subprocess.run`` but fails one chosen git/uv subcommand outright.

    Simulates e.g. a network blip on ``fetch`` (without needing a broken
    real remote) while every *other* subcommand runs for real against the
    actual repository -- the integration tests below use this to prove the
    checkout-attestation marker survives an unrelated intervening failure.
    """

    def __init__(self, fail_subcommand: str, message: str = "network unreachable") -> None:
        self.fail_subcommand = fail_subcommand
        self.message = message
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if len(argv) > 1 and argv[1] == self.fail_subcommand:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=self.message)
        return subprocess.run(argv, **kwargs)


def test_installed_reports_tag_and_commit(tmp_path: Path) -> None:
    runner = _ScriptedRunner({"rev-parse": _ok("abc1234\n"), "describe": _ok("v1.2.3\n")})
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    installed = updater.installed()

    assert installed == InstalledVersion(tag="v1.2.3", commit="abc1234")


def test_installed_reports_none_tag_when_head_is_not_on_a_tag(tmp_path: Path) -> None:
    runner = _ScriptedRunner({"rev-parse": _ok("abc1234\n"), "describe": _fail("fatal: no tag exactly matches")})
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    installed = updater.installed()

    assert installed.tag is None
    assert installed.commit == "abc1234"


def test_installed_reports_none_commit_when_not_a_git_repo(tmp_path: Path) -> None:
    runner = _ScriptedRunner({"rev-parse": _fail("fatal: not a git repository")})
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    installed = updater.installed()

    assert installed.commit is None
    assert installed.tag is None


def test_installed_uses_cwd_not_dash_c(tmp_path: Path) -> None:
    runner = _ScriptedRunner({"rev-parse": _ok("abc\n"), "describe": _ok("v1\n")})
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    updater.installed()

    for call in runner.calls:
        assert call["cwd"] == str(tmp_path)
        assert "-C" not in call["argv"]


def _happy_checkout_script(**overrides: Any) -> dict[str, Any]:
    """A script dict where fetch/status/rev-parse/checkout/sync all succeed."""
    script: dict[str, Any] = {
        "fetch": _ok(),
        "status": _ok(""),
        "rev-parse": _ok("deadbeef\n"),
        "checkout": _ok(),
        "sync": _ok(),
    }
    script.update(overrides)
    return script


def test_apply_runs_fetch_assets_checkout_sync_install_assets_in_order_with_expected_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.shutil.which", lambda name: "/usr/bin/uv")
    runner = _ScriptedRunner(_happy_checkout_script())
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, uv_bin="uv", runner=runner, opener=opener)
    steps: list[str] = []

    updater.apply("v2.0.0", on_step=steps.append)

    assert steps == ["fetch", "assets", "checkout", "sync", "install-assets"]
    argvs = [call["argv"] for call in runner.calls]
    assert argvs == [
        ["git", "fetch", "--tags", "origin"],
        ["git", "status", "--porcelain", "--untracked-files=no"],
        ["git", "rev-parse", "--verify", "--quiet", "refs/tags/v2.0.0^{commit}"],
        ["git", "checkout", "--detach", "refs/tags/v2.0.0"],
        ["/usr/bin/uv", "sync", "--project", str(tmp_path), "--frozen"],
    ]
    for call in runner.calls:
        assert call["cwd"] == str(tmp_path)
    static_dir = _static_dir(tmp_path)
    assert (static_dir / "index.html").read_bytes() == b"<html></html>"
    assert (static_dir / "assets" / "index.js").read_bytes() == b"console.log(1)"
    # No staging leftovers once install-assets has completed successfully.
    assert list(static_dir.parent.glob("static.tmp-*")) == []
    assert list(static_dir.parent.glob("static.prev")) == []


def test_apply_raises_update_step_error_with_the_failing_step_and_stderr_tail(tmp_path: Path) -> None:
    runner = _ScriptedRunner(_happy_checkout_script(checkout=_fail("error: your local changes would be overwritten")))
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)
    steps: list[str] = []

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=steps.append)

    assert excinfo.value.step == "checkout"
    assert "your local changes would be overwritten" in str(excinfo.value)
    # sync/install-assets must never have been reached once checkout failed.
    assert steps == ["fetch", "assets", "checkout"]


def test_apply_stops_before_sync_when_checkout_fails(tmp_path: Path) -> None:
    runner = _ScriptedRunner(_happy_checkout_script(checkout=_fail("boom")))
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)

    with pytest.raises(UpdateStepError):
        updater.apply("v2.0.0", on_step=lambda step: None)

    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert "sync" not in called_subcommands


def test_apply_leaves_tree_and_static_untouched_when_the_asset_step_fails(tmp_path: Path) -> None:
    """The reordered ``apply``'s whole point: a bad asset must never touch the tree or `static/`."""
    runner = _ScriptedRunner(_happy_checkout_script())

    def failing_opener(request: urllib.request.Request, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]

    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=failing_opener)
    steps: list[str] = []

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=steps.append)

    assert excinfo.value.step == "assets"
    assert steps == ["fetch", "assets"]
    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert called_subcommands == ["fetch"]  # checkout/sync never even started
    static_dir = _static_dir(tmp_path)
    assert not static_dir.exists()
    assert list(static_dir.parent.glob("static.tmp-*")) == []


def test_apply_removes_the_staging_dir_and_keeps_the_new_tag_when_sync_fails(tmp_path: Path) -> None:
    """A failed ``sync`` leaves the tree on the new tag (retryable) but never installs the staged asset."""
    runner = _ScriptedRunner(_happy_checkout_script(sync=_fail("uv sync: dependency resolution failed")))
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)
    steps: list[str] = []

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=steps.append)

    assert excinfo.value.step == "sync"
    assert steps == ["fetch", "assets", "checkout", "sync"]
    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert called_subcommands == ["fetch", "status", "rev-parse", "checkout", "sync"]  # checkout itself ran
    static_dir = _static_dir(tmp_path)
    assert not static_dir.exists()  # install-assets never ran
    assert list(static_dir.parent.glob("static.tmp-*")) == []  # staging dir was cleaned up, not left for reuse


def test_checkout_uses_the_fully_qualified_tag_ref(tmp_path: Path) -> None:
    runner = _ScriptedRunner(_happy_checkout_script())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    updater._checkout("v2.0.0", on_step=lambda step: None)

    verify_call = next(call for call in runner.calls if call["argv"][:2] == ["git", "rev-parse"])
    assert verify_call["argv"] == ["git", "rev-parse", "--verify", "--quiet", "refs/tags/v2.0.0^{commit}"]
    checkout_call = next(call for call in runner.calls if call["argv"][:2] == ["git", "checkout"])
    assert checkout_call["argv"] == ["git", "checkout", "--detach", "refs/tags/v2.0.0"]


def test_checkout_fails_when_the_tag_is_not_found_after_fetch(tmp_path: Path) -> None:
    runner = _ScriptedRunner(_happy_checkout_script(**{"rev-parse": _fail("fatal: bad revision")}))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "checkout"
    assert "tag not found after fetch" in str(excinfo.value)
    # checkout itself must never run once the tag fails to resolve.
    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert "checkout" not in called_subcommands


def test_checkout_refuses_a_dirty_working_tree(tmp_path: Path) -> None:
    runner = _ScriptedRunner(_happy_checkout_script(status=_ok(" M some/file.py\n")))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "checkout"
    assert "working tree has local changes" in str(excinfo.value)
    assert "commit, stash, or reset" in str(excinfo.value)
    # Nothing must be discarded: rev-parse/checkout never run once dirty.
    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert called_subcommands == ["status"]


def test_checkout_ignores_untracked_files_when_checking_dirtiness(tmp_path: Path) -> None:
    # --untracked-files=no is what makes this pass; the script's "status"
    # stdout would be empty for a real invocation with an untracked file
    # present, so this test only has to assert clean status still proceeds.
    runner = _ScriptedRunner(_happy_checkout_script())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    updater._checkout("v2.0.0", on_step=lambda step: None)

    status_call = next(call for call in runner.calls if call["argv"][:2] == ["git", "status"])
    assert status_call["argv"] == ["git", "status", "--porcelain", "--untracked-files=no"]


# --- checkout-attestation marker: force/refuse decisions, lifecycle ---


def test_checkout_refusal_message_starts_with_the_shared_dirty_tree_prefix(tmp_path: Path) -> None:
    # The refusal message must be BUILT FROM DIRTY_TREE_REFUSAL_PREFIX, not
    # merely happen to start with the same text -- pins that contract so a
    # reword of the message breaks this test, not the marker-gating logic
    # that shares the constant's *identity* (not its text) with the guard.
    runner = _ScriptedRunner(_happy_checkout_script(status=_ok(" M some/file.py\n")))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    message = str(excinfo.value)
    assert message.startswith(f"checkout: {DIRTY_TREE_REFUSAL_PREFIX}")


def test_checkout_forces_through_a_dirty_tree_attested_by_the_marker(tmp_path: Path) -> None:
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    marker.touch()  # attested debris from a previous checkout attempt
    runner = _ScriptedRunner(_happy_checkout_script(status=_ok(" M some/file.py\n")))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    updater._checkout("v2.0.0", on_step=lambda step: None)

    checkout_call = next(call for call in runner.calls if call["argv"][:2] == ["git", "checkout"])
    assert checkout_call["argv"] == ["git", "checkout", "--force", "--detach", "refs/tags/v2.0.0"]
    assert not marker.exists()  # success removes it


def test_checkout_refuses_a_dirty_tree_with_no_git_dir_at_all_the_same_as_no_marker(tmp_path: Path) -> None:
    # No `.git` on disk (the vast majority of tests in this file) must
    # behave exactly like "no marker" -- a bare tmp_path is not a special
    # case that accidentally allows force.
    runner = _ScriptedRunner(_happy_checkout_script(status=_ok(" M some/file.py\n")))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert DIRTY_TREE_REFUSAL_PREFIX in str(excinfo.value)


def test_checkout_creates_the_marker_immediately_before_the_mutating_checkout_call(tmp_path: Path) -> None:
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    observed: dict[str, bool] = {}

    def recording_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "checkout"]:
            observed["marker_exists_at_checkout"] = marker.exists()
        script = _happy_checkout_script()
        return script.get(argv[1], _ok())

    updater = GitUvUpdater(portal_dir=tmp_path, runner=recording_runner)

    updater._checkout("v2.0.0", on_step=lambda step: None)

    assert observed == {"marker_exists_at_checkout": True}
    assert not marker.exists()  # removed again once the checkout succeeded


def test_checkout_never_creates_a_marker_when_the_tag_is_not_found(tmp_path: Path) -> None:
    # A tag-verify failure means the tree was never touched by this
    # attempt -- creating a marker here would license a force on a later
    # retry even though nothing was ever half-applied (this is the exact
    # scenario an independent review flagged against an early draft of
    # this mechanism).
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    runner = _ScriptedRunner(_happy_checkout_script(**{"rev-parse": _fail("fatal: bad revision")}))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError):
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert not marker.exists()


def test_checkout_clears_a_stale_marker_when_the_tree_is_clean_even_if_the_tag_is_not_found(tmp_path: Path) -> None:
    # Isolates the "clean tree clears a stale marker" step from the
    # "success removes it" step: forcing a tag-not-found failure means
    # checkout never runs and never gets a chance to remove the marker
    # itself, so the marker's absence here can only be explained by the
    # up-front clean-tree clear.
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    marker.touch()  # stale, leftover from some earlier run
    runner = _ScriptedRunner(_happy_checkout_script(**{"rev-parse": _fail("fatal: bad revision")}))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError):
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert not marker.exists()


def test_checkout_keeps_the_marker_when_a_timeout_leaves_the_tree_dirty(tmp_path: Path) -> None:
    # The dirty-tree guard runs twice in this attempt: clean beforehand
    # (so the plain, unforced checkout is attempted and the marker gets
    # created), then dirty afterward once the checkout subprocess itself
    # times out mid-write -- that dirt is now attested, so the marker must
    # survive for the next attempt to force through it.
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    script = _happy_checkout_script(
        status=_seq(_ok(""), _ok(" M some/file.py\n")),
        checkout=subprocess.TimeoutExpired(cmd=["git", "checkout"], timeout=60),
    )
    runner = _ScriptedRunner(script)
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "checkout"
    assert marker.exists()


def test_checkout_removes_the_marker_when_a_failure_leaves_the_tree_clean(tmp_path: Path) -> None:
    # git refused or died before writing anything (an untracked-file
    # conflict, a bad pathspec, disk-full-before-any-write): the recheck
    # finds the tree still clean, so nothing was half-applied and the
    # marker is removed -- a retry must hit the same visible error again,
    # never silently force.
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    script = _happy_checkout_script(
        status=_seq(_ok(""), _ok("")),
        checkout=_fail("error: The following untracked working tree files would be overwritten"),
    )
    runner = _ScriptedRunner(script)
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "checkout"
    assert not marker.exists()


def test_checkout_keeps_the_marker_when_the_post_failure_recheck_itself_cannot_be_run(tmp_path: Path) -> None:
    # Erring toward keeping the marker when the recheck itself fails: a
    # forced checkout of a tree that turns out to already be clean is a
    # no-op-equivalent, while discarding the marker here risks re-wedging
    # the device the way the unconditional-refusal bug this class fixes
    # did.
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    script = _happy_checkout_script(
        status=_seq(_ok(""), _fail("fatal: index file corrupt")),
        checkout=_fail("fatal: unable to write new index file"),
    )
    runner = _ScriptedRunner(script)
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError):
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert marker.exists()


def test_apply_refuses_a_user_dirty_tree_with_no_marker_even_after_a_previous_checkout_step_failure(
    tmp_path: Path,
) -> None:
    # The scenario an earlier, broken design got wrong: a death/timeout
    # during the dirty-tree GUARD itself (e.g. a slow SD card) must never
    # attribute the tree's dirt to the updater. Since attribution is now
    # purely marker-based rather than inferred from job history, this is
    # pinned directly: a dirty tree with no marker refuses regardless of
    # what any previous job record might say.
    git_dir = _seed_git_dir(tmp_path)
    marker = git_dir / _CHECKOUT_MARKER_NAME
    assert not marker.exists()
    runner = _ScriptedRunner(_happy_checkout_script(status=_ok(" M some/file.py\n")))
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._checkout("v2.0.0", on_step=lambda step: None)

    assert DIRTY_TREE_REFUSAL_PREFIX in str(excinfo.value)
    called_subcommands = [call["argv"][1] for call in runner.calls]
    assert called_subcommands == ["status"]  # nothing forced, nothing else even attempted


# --- stale git lock sweep: gated, not unconditional ---


def test_apply_sweeps_an_old_lock_with_no_marker(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    git_dir = _seed_git_dir(tmp_path)
    lock = git_dir / "index.lock"
    lock.write_text("")
    old_mtime = time.time() - _STALE_LOCK_MIN_AGE_SECONDS - 60
    os.utime(lock, (old_mtime, old_mtime))
    runner = _ScriptedRunner(_happy_checkout_script())
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)

    with caplog.at_level(logging.WARNING):
        updater.apply("v2.0.0", on_step=lambda step: None)

    assert not lock.exists()
    assert any("index.lock" in r.message for r in caplog.records)


def test_apply_leaves_a_fresh_lock_with_no_marker_alone(tmp_path: Path) -> None:
    git_dir = _seed_git_dir(tmp_path)
    lock = git_dir / "index.lock"
    lock.write_text("")  # mtime is "now" -- well under the staleness threshold
    runner = _ScriptedRunner(_happy_checkout_script())
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)

    updater.apply("v2.0.0", on_step=lambda step: None)

    assert lock.exists()  # not attributable to this updater -- left alone


def test_apply_sweeps_a_fresh_lock_when_the_marker_is_present(tmp_path: Path) -> None:
    # The marker attests "this updater's own previous run died mid-
    # mutation" -- once that's true, the locks from that same run are its
    # own regardless of age.
    git_dir = _seed_git_dir(tmp_path)
    lock = git_dir / "index.lock"
    lock.write_text("")
    (git_dir / _CHECKOUT_MARKER_NAME).touch()
    runner = _ScriptedRunner(_happy_checkout_script())
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)

    updater.apply("v2.0.0", on_step=lambda step: None)

    assert not lock.exists()


def test_apply_warns_once_when_git_dir_is_not_a_plain_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A gitfile/worktree layout -- `.git` is not a directory at all. The
    # sweep and marker mechanisms must silently no-op rather than crash,
    # with exactly one WARNING even though both `_sweep_stale_locks` and
    # `_checkout` independently ask "is there a .git directory?".
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    runner = _ScriptedRunner(_happy_checkout_script())
    opener = _asset_opener("v2.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner, opener=opener)

    with caplog.at_level(logging.WARNING):
        updater.apply("v2.0.0", on_step=lambda step: None)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "not a plain directory" in r.message]
    assert len(warnings) == 1


def test_resolve_uv_bin_uses_shutil_which_when_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.shutil.which", lambda name: "/opt/uv/uv")
    updater = GitUvUpdater(portal_dir=tmp_path, uv_bin="uv", runner=_ScriptedRunner({}))

    assert updater._resolve_uv_bin() == "/opt/uv/uv"


def test_resolve_uv_bin_falls_back_to_local_bin_when_which_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "home" / ".local" / "bin" / "uv"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.shutil.which", lambda name: None)
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.Path.home", lambda: tmp_path / "home")
    updater = GitUvUpdater(portal_dir=tmp_path, uv_bin="uv", runner=_ScriptedRunner({}))

    assert updater._resolve_uv_bin() == str(fallback)


def test_resolve_uv_bin_raises_naming_both_attempts_when_neither_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.shutil.which", lambda name: None)
    monkeypatch.setattr("palmimo_portal.adapters.git_uv_updater.Path.home", lambda: tmp_path / "no-home")
    updater = GitUvUpdater(portal_dir=tmp_path, uv_bin="uv", runner=_ScriptedRunner({}))

    with pytest.raises(UpdateStepError) as excinfo:
        updater._resolve_uv_bin()

    assert excinfo.value.step == "sync"
    assert "uv" in str(excinfo.value)
    assert str(tmp_path / "no-home" / ".local" / "bin" / "uv") in str(excinfo.value)


def test_apply_raises_update_step_error_on_a_timeout(tmp_path: Path) -> None:
    def raising_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    updater = GitUvUpdater(portal_dir=tmp_path, runner=raising_runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "fetch"


def test_apply_raises_update_step_error_when_the_subprocess_cannot_start(tmp_path: Path) -> None:
    def raising_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git not found")

    updater = GitUvUpdater(portal_dir=tmp_path, runner=raising_runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "fetch"


def test_stderr_tail_is_truncated_to_the_last_20_lines(tmp_path: Path) -> None:
    long_stderr = "\n".join(f"line {i}" for i in range(50))
    runner = _ScriptedRunner({"fetch": _fail(long_stderr)})
    updater = GitUvUpdater(portal_dir=tmp_path, runner=runner)

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=lambda step: None)

    message = str(excinfo.value)
    assert "line 49" in message
    assert "line 29" not in message


# --- real-git integration tests: the marker mechanism against actual git semantics ---
#
# The rest of this file scripts `git` itself -- fast, but it can only prove
# the adapter behaves correctly against whatever the *script* says a git
# command returned, which is exactly how two earlier, broken attribution
# designs looked correct in review and were then broken by real git
# semantics an adversarial reviewer actually ran. These tests drive a real
# `git` binary against a real repository instead.


def test_real_git_marker_survives_an_intervening_fetch_failure_then_force_repairs(tmp_path: Path) -> None:
    """Pins the multi-job durability scenario: a killed-mid-checkout marker must outlive an unrelated later failure."""
    portal_dir = _init_real_release_repo(tmp_path)
    _publish_real_tag(tmp_path / "origin", "v2.0.0", file_name="app.py", content="v2\n")

    # Simulate the aftermath of a checkout killed mid-write: a marker left
    # behind and a genuinely dirty tracked file.
    (portal_dir / "app.py").write_text("half-written-garbage")
    marker = portal_dir / ".git" / _CHECKOUT_MARKER_NAME
    marker.touch()

    # A second apply that fails at "fetch" (a network blip) must not touch
    # the marker or the dirty tree at all -- the failure happens before
    # checkout is ever reached.
    failing_fetch = _FailingStepRunner("fetch")
    updater = GitUvUpdater(
        portal_dir=portal_dir, runner=failing_fetch, opener=_asset_opener("v2.0.0", _minimal_static_tar())
    )
    with pytest.raises(UpdateStepError) as first:
        updater.apply("v2.0.0", on_step=lambda step: None)
    assert first.value.step == "fetch"
    assert marker.exists()
    assert (portal_dir / "app.py").read_text() == "half-written-garbage"

    # A third apply -- fetch/checkout for real, sync deliberately failing
    # so this test doesn't need a real `uv sync` -- must still
    # force-repair through the attested dirt.
    failing_sync = _FailingStepRunner("sync")
    updater2 = GitUvUpdater(
        portal_dir=portal_dir, runner=failing_sync, opener=_asset_opener("v2.0.0", _minimal_static_tar())
    )
    with pytest.raises(UpdateStepError) as second:
        updater2.apply("v2.0.0", on_step=lambda step: None)
    assert second.value.step == "sync"
    assert not marker.exists()
    assert (portal_dir / "app.py").read_text() == "v2\n"


def test_real_git_untracked_conflict_refuses_both_times_without_ever_clobbering_the_operators_file(
    tmp_path: Path,
) -> None:
    """Pins the untracked-conflict scenario: git's own refusal must never be silently forced away on retry."""
    portal_dir = _init_real_release_repo(tmp_path)
    _publish_real_tag(tmp_path / "origin", "v2.0.0", file_name="conflict.txt", content="from-v2\n")
    (portal_dir / "conflict.txt").write_text("operator-data\n")  # untracked, operator-owned
    marker = portal_dir / ".git" / _CHECKOUT_MARKER_NAME
    updater = GitUvUpdater(portal_dir=portal_dir, opener=_asset_opener("v2.0.0", _minimal_static_tar()))

    with pytest.raises(UpdateStepError) as first:
        updater.apply("v2.0.0", on_step=lambda step: None)
    assert first.value.step == "checkout"
    assert not marker.exists()
    assert (portal_dir / "conflict.txt").read_text() == "operator-data\n"

    with pytest.raises(UpdateStepError) as second:
        updater.apply("v2.0.0", on_step=lambda step: None)
    assert second.value.step == "checkout"
    assert not marker.exists()
    assert (portal_dir / "conflict.txt").read_text() == "operator-data\n"


def test_real_git_tag_not_found_never_creates_a_marker_so_a_later_operator_edit_is_never_forced_away(
    tmp_path: Path,
) -> None:
    """The tree was never touched by a tag-not-found failure -- a later operator edit must still refuse, not force."""
    portal_dir = _init_real_release_repo(tmp_path)
    # v2.0.0 is never published -- fetch succeeds, but the tag simply does
    # not exist.
    marker = portal_dir / ".git" / _CHECKOUT_MARKER_NAME
    updater = GitUvUpdater(portal_dir=portal_dir, opener=_asset_opener("v2.0.0", _minimal_static_tar()))

    with pytest.raises(UpdateStepError) as first:
        updater.apply("v2.0.0", on_step=lambda step: None)
    assert first.value.step == "checkout"
    assert "tag not found after fetch" in str(first.value)
    assert not marker.exists()

    # The operator now edits a tracked file over SSH, in the gap between
    # attempts.
    (portal_dir / "app.py").write_text("operator's own edit\n")

    with pytest.raises(UpdateStepError) as second:
        updater.apply("v2.0.0", on_step=lambda step: None)
    assert second.value.step == "checkout"
    assert DIRTY_TREE_REFUSAL_PREFIX in str(second.value)  # refused, never forced
    assert not marker.exists()
    assert (portal_dir / "app.py").read_text() == "operator's own edit\n"


def test_real_git_fresh_lock_with_no_marker_makes_checkout_fail_visibly_and_the_lock_survives(tmp_path: Path) -> None:
    """A live operator lock (a concurrent `git add`/commit over SSH) must never be swept -- the git call fails instead."""
    portal_dir = _init_real_release_repo(tmp_path)
    _publish_real_tag(tmp_path / "origin", "v2.0.0", file_name="app.py", content="v2\n")
    lock = portal_dir / ".git" / "index.lock"
    lock.write_text("")  # fresh -- e.g. an operator's own concurrent `git add` over SSH
    updater = GitUvUpdater(portal_dir=portal_dir, opener=_asset_opener("v2.0.0", _minimal_static_tar()))

    with pytest.raises(UpdateStepError) as excinfo:
        updater.apply("v2.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "checkout"
    assert "File exists" in str(excinfo.value)
    assert lock.exists()  # never attributed to this updater -- left for the operator's own command


# --- the "assets" step: download, verify, and safely stage the frontend build ---


def test_assets_downloads_verifies_and_stages_without_touching_static(tmp_path: Path) -> None:
    tar_bytes = _minimal_static_tar()
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)
    steps: list[str] = []

    temp_dir = updater._assets("v3.0.0", on_step=steps.append)

    assert steps == ["assets"]
    assert (temp_dir / "index.html").read_bytes() == b"<html></html>"
    assert (temp_dir / "assets" / "index.js").read_bytes() == b"console.log(1)"
    # static/ itself must not exist yet -- only install-assets touches it.
    assert not _static_dir(tmp_path).exists()


def test_assets_raises_update_step_error_on_checksum_mismatch(tmp_path: Path) -> None:
    tar_bytes = _minimal_static_tar()
    opener = _asset_opener("v3.0.0", tar_bytes, sha256_hex="0" * 64)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "checksum mismatch" in str(excinfo.value)
    # A bad checksum must never leave a static/ directory or a staging dir behind.
    static_dir = _static_dir(tmp_path)
    assert not static_dir.exists()
    assert list(static_dir.parent.glob("static.tmp-*")) == []


def test_assets_rejects_a_traversal_member(tmp_path: Path) -> None:
    tar_bytes = _build_tar({"static/../../../etc/passwd": b"pwned"})
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "unsafe member" in str(excinfo.value)


def test_assets_rejects_a_member_outside_the_static_prefix(tmp_path: Path) -> None:
    tar_bytes = _build_tar({"not-static/index.html": b"<html></html>"})
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "does not resolve under static/" in str(excinfo.value)


def test_assets_rejects_a_symlink_member(tmp_path: Path) -> None:
    link = tarfile.TarInfo(name="static/evil-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    tar_bytes = _build_tar({}, extra=[link])
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "not a file or directory" in str(excinfo.value)


def test_assets_rejects_a_device_file_member(tmp_path: Path) -> None:
    device = tarfile.TarInfo(name="static/evil-device")
    device.type = tarfile.CHRTYPE
    tar_bytes = _build_tar({}, extra=[device])
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError):
        updater._assets("v3.0.0", on_step=lambda step: None)


def test_assets_rejects_a_member_whose_header_claims_a_huge_uncompressed_size(tmp_path: Path) -> None:
    """A gzip-bomb shape: a small download whose header claims an uncompressed size above the per-member cap.

    Uses real (highly compressible, all-zero) content of that size rather
    than a lying header with no matching data -- ``tarfile`` needs to be
    able to actually walk the archive to enumerate its members at all -- so
    this proves the cap rejects the member from its declared
    ``TarInfo.size`` before ever reading/writing its content, while the
    *downloaded* tarball itself stays tiny.
    """
    oversized_member = b"\x00" * (MEMBER_MAX_BYTES + 1)
    tar_bytes = _build_tar({"static/bomb.bin": oversized_member})
    assert len(tar_bytes) < len(oversized_member) // 100, (
        "the compressed tarball should be far smaller than its claimed uncompressed size for this to be a "
        "meaningful bomb test"
    )
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "expands beyond" in str(excinfo.value)
    static_dir = _static_dir(tmp_path)
    assert not static_dir.exists()
    assert list(static_dir.parent.glob("static.tmp-*")) == []


def test_assets_rejects_when_the_running_total_of_several_members_exceeds_the_total_cap(tmp_path: Path) -> None:
    """Several members, each under the per-member cap, whose combined size still exceeds the total cap."""
    chunk = b"\x00" * MEMBER_MAX_BYTES  # each individually allowed; one shared object, not 11 separate allocations
    members = {f"static/chunk-{i}.bin": chunk for i in range(11)}  # 11 * 20 MB > 200 MB total cap
    tar_bytes = _build_tar(members)
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "expands beyond" in str(excinfo.value)
    static_dir = _static_dir(tmp_path)
    assert not static_dir.exists()
    assert list(static_dir.parent.glob("static.tmp-*")) == []


def test_assets_raises_update_step_error_with_a_clear_message_on_404(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]

    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener, update_repo="Jizai-inc/palmimo-portal")

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v9.9.9", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    message = str(excinfo.value)
    assert "v9.9.9" in message
    assert "publish it before devices can update" in message
    assert STATIC_ASSET_NAME_TEMPLATE.format(tag="v9.9.9") in message


def test_assets_raises_update_step_error_on_a_download_timeout(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> Any:
        raise TimeoutError("timed out")

    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert not list((tmp_path / "palmimo_portal").glob("static.tmp-*"))


def test_assets_raises_update_step_error_on_a_connection_reset(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> Any:
        raise urllib.error.URLError(ConnectionResetError("connection reset by peer"))

    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"


def test_assets_raises_update_step_error_when_the_download_exceeds_the_size_cap(tmp_path: Path) -> None:
    oversized = b"x" * (ASSET_MAX_BYTES + 1)

    def opener(request: urllib.request.Request, timeout: float) -> Any:
        return _FakeAssetResponse(oversized)

    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)

    with pytest.raises(UpdateStepError) as excinfo:
        updater._assets("v3.0.0", on_step=lambda step: None)

    assert excinfo.value.step == "assets"
    assert "size cap" in str(excinfo.value) or "byte" in str(excinfo.value)


def test_assets_uses_the_configured_update_repo_in_the_download_url(tmp_path: Path) -> None:
    seen_urls: list[str] = []

    def opener(request: urllib.request.Request, timeout: float) -> Any:
        seen_urls.append(request.full_url)
        if request.full_url.endswith(".sha256"):
            digest = hashlib.sha256(_minimal_static_tar()).hexdigest()
            asset_name = STATIC_ASSET_NAME_TEMPLATE.format(tag="v3.0.0")
            return _FakeAssetResponse(f"{digest}  {asset_name}\n".encode())
        return _FakeAssetResponse(_minimal_static_tar())

    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener, update_repo="acme/example")

    updater._assets("v3.0.0", on_step=lambda step: None)

    asset_name = STATIC_ASSET_NAME_TEMPLATE.format(tag="v3.0.0")
    expected = f"https://github.com/acme/example/releases/download/v3.0.0/{asset_name}"
    assert expected in seen_urls
    assert f"{expected}.sha256" in seen_urls


# --- the "install-assets" step: atomically swap the staged build into static/ ---


def test_install_assets_swaps_the_staged_dir_into_static(tmp_path: Path) -> None:
    tar_bytes = _minimal_static_tar()
    opener = _asset_opener("v3.0.0", tar_bytes)
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)
    temp_dir = updater._assets("v3.0.0", on_step=lambda step: None)
    steps: list[str] = []

    updater._install_assets(temp_dir, on_step=steps.append)

    assert steps == ["install-assets"]
    static_dir = _static_dir(tmp_path)
    assert (static_dir / "index.html").read_bytes() == b"<html></html>"
    assert (static_dir / "assets" / "index.js").read_bytes() == b"console.log(1)"
    assert not temp_dir.exists()
    assert list(static_dir.parent.glob("static.tmp-*")) == []
    assert list(static_dir.parent.glob("static.prev")) == []


def test_install_assets_replaces_an_existing_static_dir(tmp_path: Path) -> None:
    static_dir = _static_dir(tmp_path)
    static_dir.mkdir(parents=True)
    (static_dir / "stale.html").write_text("old build")
    opener = _asset_opener("v3.0.0", _minimal_static_tar())
    updater = GitUvUpdater(portal_dir=tmp_path, opener=opener)
    temp_dir = updater._assets("v3.0.0", on_step=lambda step: None)

    updater._install_assets(temp_dir, on_step=lambda step: None)

    assert not (static_dir / "stale.html").exists()
    assert (static_dir / "index.html").read_bytes() == b"<html></html>"
