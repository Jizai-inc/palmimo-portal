"""Real :class:`~palmimo_portal.ports.Updater`: ``fetch`` -> ``assets`` -> ``checkout`` -> ``sync`` -> ``install-assets``.

Applies one GitHub Release tag to the Portal checkout (this repository's
clone on the device) this process is itself running out of
(``settings.portal_dir``). The ``fetch``/``checkout``/
``sync`` steps shell out through ``runner`` (default :func:`subprocess.run`),
matching the allowed-dependency list for this feature; ``runner`` is the test
seam (``tests/test_git_uv_updater_adapter.py`` asserts argv/cwd/order with a
fake). The ``assets``/``install-assets`` steps download and unpack the
frontend's build output from the same GitHub Release via ``urllib`` (stdlib)
-- the device never runs Node -- through ``opener``, the same test-seam shape
:class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource` uses;
the download/verify/extract/swap logic itself lives in
:mod:`palmimo_portal.adapters.static_asset`, shared with
:mod:`palmimo_portal.fetch_static` (the developer-facing CLI equivalent).

Step order is ``fetch -> assets -> checkout -> sync -> install-assets``, not
``fetch -> checkout -> assets -> sync``:

1. **``fetch``** -- ``git fetch --tags origin``, so the tag exists locally
   to check out and verify.
2. **``assets``** -- download the frontend tarball, verify its checksum, and
   extract it into a staging directory (``static.tmp-<pid>``, a sibling of
   ``static/``) -- *before* the working tree is touched. Doing this first
   means a 404, checksum mismatch, or malformed/oversized tarball is caught
   while the Portal checkout and ``static/`` are both untouched -- nothing
   has to be undone.
3. **``checkout``** -- the dirty-tree guard, tag-exists verification, and
   ``git checkout --detach``. Only reached once step 2 has proven a usable
   frontend build exists for this tag. See "Being killed mid-``checkout``"
   below for how this step tells updater-inflicted dirt apart from a
   human's.
4. **``sync``** -- ``uv sync --frozen`` against the now-checked-out tree.
5. **``install-assets``** -- the atomic swap of the step-2 staging
   directory into ``static/``, done **last**, once ``sync`` has succeeded:
   the running Portal process keeps serving the *old* ``static/`` and old
   code right up until the swap and the subsequent
   :class:`~palmimo_portal.core.update_runner.UpdateRunner` restart, so
   there is never a window where a half-updated backend serves a
   mismatched frontend.

If ``checkout`` or ``sync`` fails, the step-2 staging directory is removed (a
retry re-downloads rather than reusing a possibly-stale directory) while the
working tree is left where ``checkout`` put it; ``static/`` is untouched
either way, since step 5 never ran. A retry re-applies the same target from
there.

Rollback goes through the same :meth:`GitUvUpdater.apply` entry point as a
forward update (``api/update.py`` picks the tag) -- rolling back re-downloads
that tag's own frontend asset rather than restoring a saved ``static/``.

**Being killed mid-``checkout``: the checkout-attestation marker.** ``fetch``
and ``checkout`` run as subprocesses under 120s/60s timeouts that SIGKILL
them on expiry; a power cut can hit either with no warning at all. Either can
leave stale ``.git`` lock files behind (see "Stale git locks" below); a
killed ``checkout`` specifically can also leave a half-switched, dirty
working tree. The dirty-tree guard (``git status --porcelain
--untracked-files=no``) exists precisely to stop the updater from touching a
tree a *human* modified over SSH -- that refusal, and the manual-resolution
burden it puts on an operator, is a deliberate, accepted risk that must never
be bypassed for a genuinely USER-dirty tree. But naively trusting *any*
previous failure record to distinguish "the updater's own debris" from "a
human's edit" does not hold up: the persisted job history is inferred after
the fact and can be stale, incomplete, or itself ambiguous (a design this
class went through, and an adversarial review broke with working
proofs-of-concept -- see "Review history" at the bottom of this docstring).

Instead, :meth:`_checkout` uses a **marker file created by this same checkout
attempt, immediately before the one command that can actually mutate the
tree**: ``git checkout``. The marker lives outside the worktree, at
``<portal_dir>/.git/palmimo-checkout-in-progress``, so it survives whatever
``git checkout`` itself does to tracked files.

1. Run the dirty-tree guard. If the tree is dirty:

   - **Marker present** -- this dirt is *this process's own* attested debris
     from a checkout that started and did not confirm its own outcome (killed
     mid-write, or the process died before recording success/failure). Run
     ``git checkout --force --detach refs/tags/<tag>``, which clobbers the
     half-applied state with the already-validated tag.
   - **No marker** -- refuse, exactly as before (:data:`DIRTY_TREE_REFUSAL_PREFIX`).
     Nothing here attests that this dirt is the updater's; it is treated as a
     human's and left alone.

2. If the tree is clean, remove any stale marker (a clean tree needs no
   repair) and proceed to resolve the tag.
3. Resolve ``tag`` against ``refs/tags/<tag>^{commit}``. If this fails ("tag
   not found after fetch"), the tree was **never touched** by this attempt --
   no marker is created here, so a later retry cannot force through a tree an
   operator dirties in the meantime (see "Review history" -- this exact
   ordering closes a proof-of-concept an independent review raised against
   an earlier draft of this mechanism).
4. **Only now**, immediately before invoking ``git checkout`` itself, create
   the marker (a plain "clean" checkout also creates it, since the upcoming
   ``git checkout`` is the one command that can leave the tree dirty even
   without ``--force``, e.g. a mid-write kill).
5. Run the ``git checkout`` (forced or not, per step 1). Once it returns (or
   raises, including a timeout):

   - **Success** -- remove the marker.
   - **Failure or timeout** -- re-run the dirty-tree guard.

     - **Still clean** -- ``git`` refused or died before writing anything
       (e.g. an untracked-file conflict, a bad pathspec, a full disk before
       any write): nothing was half-applied, so the marker is removed. A
       retry takes the plain (non-forced) path and hits the same visible
       error again, until an operator resolves it -- it must never be
       silently forced away.
     - **Dirty** -- that dirt is now attested; the marker is kept, and the
       next attempt force-repairs through it. A process death/power cut
       between marker-creation and this recheck is the one window that
       leaves the marker behind without this code ever running that
       decision -- which is exactly the case ``--force`` exists for.
     - **The recheck itself cannot be run** -- erring toward *keeping* the
       marker is the safe choice: a forced checkout of a tree that turns out
       to already be clean is a no-op-equivalent, while discarding the
       marker here would risk re-wedging the device the same way the
       unconditional-refusal bug this class fixes did.

Marker lifecycle, audited in full: created only in step 4 (after tag
resolution succeeds, immediately before the mutating ``git checkout``
call); removed in step 2 (stale, tree already clean), on success in step 5,
and on the step-5 "still clean" recheck. No other code creates or removes
it. A ``.git`` that is not a plain directory (a gitfile -- submodule or
worktree layout) makes the marker (and the lock sweep below) a silent no-op;
this is logged once at WARNING, since the device deploy contract is a plain
``git clone``.

**Residual accepted risk:** an operator who edits the tree over SSH *after*
a power cut mid-checkout (marker left behind, tree dirty) but *before* the
next retry loses those edits to the forced checkout. This is narrow -- it
requires deliberately editing a tree that already holds updater debris from
an interrupted run -- and is documented here rather than engineered away,
since doing so would require the operator to signal "I am about to touch
this" through some channel the Portal has no way to observe.

**Stale git locks: a gated sweep, not an unconditional one.**
:meth:`_sweep_stale_locks` runs at the start of every ``apply``/rollback,
before ``fetch`` -- a killed ``git fetch``/``git checkout`` can leave
``.git/index.lock`` and friends behind, which makes every later ``git``
invocation in this checkout fail with "File exists" forever. But locks are
*not* always safe to delete on sight: an operator's own ``git`` command
running concurrently over SSH (a commit, a stash) holds a real, live lock,
and deleting it out from under that command can corrupt the operator's own
work. A lock is removed only when it is attributable to this updater by one
of two signals:

- its mtime is older than :data:`_STALE_LOCK_MIN_AGE_SECONDS` (comfortably
  longer than every step timeout this class uses, and longer than any
  plausible interactive git command an operator might run by hand), or
- the checkout-attestation marker above exists, meaning a previous run of
  *this updater* provably died mid-mutation -- the locks from that same run
  are its own regardless of age.

A lock that matches neither is left alone; the git call that needs it then
fails visibly with git's own "File exists" error, and a later retry (once
the lock is genuinely stale, or the operator's own command has finished)
succeeds. One WARNING is logged listing what was actually removed.

**Review history.** This mechanism replaces two earlier designs, both of
which were broken by an adversarial review armed with working
proofs-of-concept before landing:

1. *Attribute any previous job that failed at ``"fetch"`` or ``"checkout"``.*
   Broken because the dirty-tree refusal is itself recorded as a failed job
   at step ``"checkout"``, indistinguishable by step alone from a killed
   checkout -- a USER-dirty tree refused once would be force-clobbered on
   the very next apply.
2. *Same, but exclude jobs whose persisted error is the refusal message
   (``core.update.DIRTY_TREE_REFUSAL_PREFIX``).* Still broken, three ways:
   the "last job" record is a single slot that an unrelated later failure
   (e.g. a transient ``fetch`` network blip) silently overwrites, permanently
   erasing the evidence a genuinely repairable failure needs and re-wedging
   the device -- the exact outcome this class exists to prevent; an
   operator's *untracked* file that a new tag ships as tracked makes ``git
   checkout`` fail with its own "untracked working tree files would be
   overwritten" error, which carries no refusal-prefix marker and so was
   wrongly attributed to the updater, silently overwriting the operator's
   file on the next apply; and the previous-job step was recorded via
   ``on_step("checkout")`` *before* the dirty-tree guard itself ran, so a
   death/timeout during the guard (e.g. a slow SD card) would attribute a
   genuinely USER-dirty tree to the updater. Inferring attribution from a
   job-history record proved unable to be made sound. This mechanism instead
   attests directly, in the filesystem, at the moment the tree is actually
   about to be mutated -- no history to overwrite, no ambiguity about which
   failure produced which dirt.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from palmimo_portal.adapters.static_asset import (
    Opener,
    StaticAssetError,
    default_opener,
    fetch_and_stage,
    swap_into_place,
)
from palmimo_portal.core.update import STATIC_ASSET_NAME_TEMPLATE
from palmimo_portal.ports import InstalledVersion, Updater, UpdateStepError
from palmimo_portal.version import portal_version


logger = logging.getLogger("palmimo_portal")

#: Step timeouts, in order of :meth:`GitUvUpdater.apply`'s own steps.
#: ``sync`` gets the longest budget: ``uv sync`` can rebuild native wheels
#: (dbus-fast, argon2-cffi) on a Pi.
FETCH_TIMEOUT_SECONDS = 120.0
CHECKOUT_TIMEOUT_SECONDS = 60.0
SYNC_TIMEOUT_SECONDS = 600.0

#: Trailing stderr lines :class:`~palmimo_portal.ports.UpdateStepError`
#: carries -- enough to show the git/uv failure without unbounded output in
#: persisted state (``update.json``) or a log line.
_STDERR_TAIL_LINES = 20

#: The stable prefix of the dirty-tree guard's refusal message -- kept as a
#: named constant (rather than an inline literal at each raise site) so a
#: test can pin the exact wording contract without duplicating it.
DIRTY_TREE_REFUSAL_PREFIX = "working tree has local changes"

#: Name of the checkout-attestation marker file, created under ``.git/``
#: (never inside the worktree, so ``git checkout`` itself cannot touch it)
#: immediately before the one command that can mutate the tree. See the
#: module docstring's "Being killed mid-``checkout``" section.
_CHECKOUT_MARKER_NAME = "palmimo-checkout-in-progress"

#: Minimum age, in seconds, before :meth:`GitUvUpdater._sweep_stale_locks`
#: will remove a git lock file it cannot otherwise attribute to this updater
#: via the checkout marker. Comfortably longer than every step timeout this
#: class uses (fetch 120s, checkout 60s) and longer than any plausible
#: interactive ``git`` command an operator might be running by hand over
#: SSH.
_STALE_LOCK_MIN_AGE_SECONDS = 600.0

#: Git lock files directly under ``.git/`` a killed ``git fetch``/``git
#: checkout`` can leave behind -- see :meth:`GitUvUpdater._sweep_stale_locks`.
_GIT_DIR_LOCK_NAMES = ("index.lock", "packed-refs.lock", "shallow.lock", "HEAD.lock")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _stderr_tail(stderr: str) -> str:
    lines = stderr.splitlines()
    return "\n".join(lines[-_STDERR_TAIL_LINES:]).strip()


@dataclass
class GitUvUpdater(Updater):
    """Applies a release tag to ``portal_dir`` via ``git``/``uv`` subprocesses and a GitHub Release asset."""

    portal_dir: Path
    uv_bin: str = "uv"
    runner: Runner = field(default=subprocess.run)
    #: ``owner/repo`` the ``assets`` step downloads the release tarball from,
    #: same as :class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource`
    #: (``settings.update_repo``).
    update_repo: str = "Jizai-inc/palmimo-portal"
    opener: Opener = field(default=default_opener)
    _warned_not_git: bool = field(default=False, init=False, repr=False)
    _warned_not_plain_clone: bool = field(default=False, init=False, repr=False)

    def installed(self) -> InstalledVersion:
        commit = self._run_git_capture(["git", "rev-parse", "--short", "HEAD"])
        if commit is None and not self._warned_not_git:
            logger.warning(
                "portal_dir=%s does not look like a git checkout -- installed().commit is None", self.portal_dir
            )
            self._warned_not_git = True
        tag = self._run_git_capture(["git", "describe", "--tags", "--exact-match", "HEAD"])
        return InstalledVersion(tag=tag, commit=commit)

    def _run_git_capture(self, argv: list[str]) -> str | None:
        try:
            result = self.runner(
                argv, cwd=str(self.portal_dir), capture_output=True, text=True, timeout=CHECKOUT_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.warning("git command %s failed: %s", argv, error)
            return None
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        return output or None

    def apply(self, tag: str, on_step: Callable[[str], None]) -> None:
        self._sweep_stale_locks()
        self._step("fetch", ["git", "fetch", "--tags", "origin"], FETCH_TIMEOUT_SECONDS, on_step)
        temp_dir = self._assets(tag, on_step)
        try:
            self._checkout(tag, on_step)
            self._sync(on_step)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        self._install_assets(temp_dir, on_step)

    # -- checkout-attestation marker ------------------------------------------------------

    def _git_dir(self) -> Path | None:
        """Return ``.git``, or ``None`` (logging once) when it is not a plain directory.

        A gitfile (submodule/worktree layout) makes the marker and the lock
        sweep silent no-ops -- see the module docstring. The device deploy
        contract is a plain ``git clone``.
        """
        git_dir = self.portal_dir / ".git"
        if git_dir.is_dir():
            return git_dir
        if not self._warned_not_plain_clone:
            logger.warning(
                "portal_dir=%s .git is not a plain directory (gitfile/worktree layout) -- "
                "the stale-lock sweep and checkout-attestation marker are no-ops here; "
                "the device deploy contract is a plain `git clone`",
                self.portal_dir,
            )
            self._warned_not_plain_clone = True
        return None

    def _checkout_marker_path(self) -> Path | None:
        git_dir = self._git_dir()
        if git_dir is None:
            return None
        return git_dir / _CHECKOUT_MARKER_NAME

    def _working_tree_is_dirty(self) -> bool:
        status = self._run_or_raise(
            "checkout", ["git", "status", "--porcelain", "--untracked-files=no"], CHECKOUT_TIMEOUT_SECONDS
        )
        if status.returncode != 0:
            raise UpdateStepError(
                "checkout", _stderr_tail(status.stderr or "") or f"git status exited {status.returncode}"
            )
        return bool(status.stdout.strip())

    def _resolve_marker_after_checkout_failure(self, marker: Path) -> None:
        """Decide, after a ``checkout``-step failure, whether the marker still attests real damage.

        See the module docstring's step 5. Never raises -- this runs from an
        ``except`` block that is about to re-raise the original failure.
        """
        try:
            still_dirty = self._working_tree_is_dirty()
        except UpdateStepError:
            logger.warning(
                "could not re-check working-tree cleanliness after a checkout failure -- keeping the checkout marker"
            )
            return
        if not still_dirty:
            marker.unlink(missing_ok=True)

    def _checkout(self, tag: str, on_step: Callable[[str], None]) -> None:
        """Refuse a dirty checkout unless it is attested updater debris, then check the tag out.

        See the module docstring's "Being killed mid-``checkout``" section
        for the full marker-based mechanism this implements.
        """
        on_step("checkout")
        marker = self._checkout_marker_path()
        force = False
        if self._working_tree_is_dirty():
            if marker is not None and marker.exists():
                force = True
            else:
                raise UpdateStepError(
                    "checkout",
                    f"{DIRTY_TREE_REFUSAL_PREFIX}; commit, stash, or reset them over SSH before updating",
                )
        elif marker is not None:
            marker.unlink(missing_ok=True)  # a clean tree needs no repair -- clear any stale marker

        ref = f"refs/tags/{tag}"
        verify = self._run_or_raise(
            "checkout", ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], CHECKOUT_TIMEOUT_SECONDS
        )
        if verify.returncode != 0:
            # The tree was never touched by this attempt -- no marker is
            # created here, precisely so a later retry cannot force through
            # a tree an operator dirties between now and then.
            raise UpdateStepError("checkout", "tag not found after fetch")

        # Create the marker only now, as late as possible: immediately
        # before the one command that can actually mutate the tree.
        if marker is not None:
            marker.touch()

        checkout_argv = ["git", "checkout", "--detach", ref]
        if force:
            checkout_argv.insert(2, "--force")
        try:
            checkout = self._run_or_raise("checkout", checkout_argv, CHECKOUT_TIMEOUT_SECONDS)
        except UpdateStepError:
            if marker is not None:
                self._resolve_marker_after_checkout_failure(marker)
            raise
        if checkout.returncode != 0:
            if marker is not None:
                self._resolve_marker_after_checkout_failure(marker)
            raise UpdateStepError(
                "checkout", _stderr_tail(checkout.stderr or "") or f"checkout {ref} exited {checkout.returncode}"
            )
        if marker is not None:
            marker.unlink(missing_ok=True)

    # -- stale git lock sweep --------------------------------------------------------------

    def _sweep_stale_locks(self) -> None:
        """Remove git lock files this updater can attribute to itself, before every apply/rollback run.

        See the module docstring's "Stale git locks" section: a lock is
        removed only when it is older than :data:`_STALE_LOCK_MIN_AGE_SECONDS`
        or the checkout marker is present (this updater's own previous run
        provably died mid-mutation). A fresh, unattributed lock is left
        alone -- the git call that needs it fails visibly instead of this
        method silently deleting a live operator lock out from under a
        concurrent SSH session.
        """
        git_dir = self._git_dir()
        if git_dir is None:
            return
        marker_present = (git_dir / _CHECKOUT_MARKER_NAME).exists()
        candidates = [git_dir / name for name in _GIT_DIR_LOCK_NAMES]
        refs_dir = git_dir / "refs"
        if refs_dir.is_dir():
            candidates.extend(sorted(refs_dir.rglob("*.lock")))
        now = time.time()
        removed: list[str] = []
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                attributable = marker_present or (now - path.stat().st_mtime) >= _STALE_LOCK_MIN_AGE_SECONDS
                if not attributable:
                    continue
                path.unlink()
                removed.append(str(path.relative_to(self.portal_dir)))
            except OSError as error:
                logger.warning("could not remove stale git lock %s: %s", path, error)
        if removed:
            logger.warning("removed stale git lock file(s) before update: %s", ", ".join(removed))

    def _static_dir(self) -> Path:
        # Mirrors settings.DEFAULT_STATIC_DIR's path shape, resolved against
        # `portal_dir` -- must stay in sync with that module and with
        # app.py's static resolution (`_mount_frontend`).
        return self.portal_dir / "palmimo_portal" / "static"

    def _assets(self, tag: str, on_step: Callable[[str], None]) -> Path:
        """Download, verify, and unpack the frontend build's Release asset into a staging directory.

        Runs before ``checkout``/``sync`` touch the tree -- see the module
        docstring. Returns the staging directory, not yet swapped into
        ``static/`` (:meth:`_install_assets` does that); the caller removes
        it if a later step fails.
        """
        on_step("assets")
        asset_name = STATIC_ASSET_NAME_TEMPLATE.format(tag=tag)
        temp_dir = self._static_dir().parent / f"static.tmp-{os.getpid()}"
        not_found_message = f"release {tag} has no frontend asset {asset_name} -- publish it before devices can update"
        try:
            fetch_and_stage(
                self.opener,
                self.update_repo,
                tag,
                asset_name,
                f"palmimo-portal/{portal_version()}",
                temp_dir,
                not_found_message=not_found_message,
            )
        except StaticAssetError as error:
            raise UpdateStepError("assets", str(error)) from error
        logger.info("assets: downloaded, verified, and staged %s", asset_name)
        return temp_dir

    def _install_assets(self, temp_dir: Path, on_step: Callable[[str], None]) -> None:
        """Atomically swap the staged frontend build (from :meth:`_assets`) into ``static/``.

        Done last, after ``sync`` succeeds -- see the module docstring.
        """
        on_step("install-assets")
        try:
            swap_into_place(temp_dir, self._static_dir())
        except StaticAssetError as error:
            raise UpdateStepError("install-assets", str(error)) from error

    def _sync(self, on_step: Callable[[str], None]) -> None:
        on_step("sync")
        uv_path = self._resolve_uv_bin()
        result = self._run_or_raise(
            "sync", [uv_path, "sync", "--project", str(self.portal_dir), "--frozen"], SYNC_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            raise UpdateStepError("sync", _stderr_tail(result.stderr or "") or f"uv sync exited {result.returncode}")

    def _resolve_uv_bin(self) -> str:
        """Resolve :attr:`uv_bin` to an executable path.

        A bare ``"uv"`` (the default) is not reliably on ``PATH`` for a
        systemd unit, which does not necessarily include wherever the
        operator's shell put ``uv`` (commonly ``~/.local/bin``, from the
        official installer). :func:`shutil.which` is tried first, so an
        operator who did wire up ``PATH`` (or set ``PALMIMO_UV_BIN``) is
        respected as-is; the ``~/.local/bin/uv`` fallback covers the common
        case otherwise.

        Raises:
            UpdateStepError: neither attempt found an executable -- names
                both paths tried, so the message is actionable on-device.
        """
        found = shutil.which(self.uv_bin)
        if found is not None:
            return found
        fallback = Path.home() / ".local" / "bin" / "uv"
        if fallback.exists():
            return str(fallback)
        raise UpdateStepError(
            "sync",
            f"uv not found: shutil.which({self.uv_bin!r}) failed and {fallback} does not exist",
        )

    def _run_or_raise(self, step: str, argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(argv, cwd=str(self.portal_dir), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise UpdateStepError(step, f"{' '.join(argv)} timed out after {timeout:g}s") from error
        except OSError as error:
            raise UpdateStepError(step, f"{' '.join(argv)} failed to start: {error}") from error

    def _step(self, step: str, argv: list[str], timeout: float, on_step: Callable[[str], None]) -> None:
        on_step(step)
        result = self._run_or_raise(step, argv, timeout)
        if result.returncode != 0:
            raise UpdateStepError(
                step, _stderr_tail(result.stderr or "") or f"{' '.join(argv)} exited {result.returncode}"
            )
