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
   repair) and proceed to resolve the tag. This same check also runs once,
   unconditionally, at the very *start* of :meth:`apply` -- before
   ``fetch`` -- not only inside this step; see "A clean tree at apply-start
   proves any marker stale" below for why that duplication is load-bearing,
   not defensive fluff.
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
call); removed in step 2 (stale, tree already clean), at the start of
:meth:`apply` (see immediately below), on success in step 5, and on the
step-5 "still clean" recheck. No other code creates or removes it, and its
creation is fsynced together with the ``.git`` directory entry (mirroring
:func:`~palmimo_portal.adapters.atomic_write.atomic_write_text`'s
discipline), so a power cut immediately after cannot lose the marker while
the half-checkout dirt it attests to survives -- that would silently
recreate the exact wedge this class exists to fix. A ``.git`` that is not a
plain directory (a gitfile -- submodule or worktree layout) makes the
marker (and the lock sweep below) a silent no-op; this is logged once at
WARNING, since the device deploy contract is a plain ``git clone``.

**A clean tree at apply-start proves any marker stale.** The marker's
meaning is "the tree's *current* dirt is attested updater debris" -- not
"a checkout attempt once failed at some point in the past." A marker that
outlives the dirt it was created for (the checkout that left it eventually
gets cleaned up by a human, or the tree was fixed some other way) is no
longer evidence of anything, and a proof-of-concept confirmed it can
otherwise linger for days: a kill between checkout-success and the marker's
own removal, or between the marker's creation and the checkout even
starting, leaves a marker sitting next to a tree that later *becomes*
clean again through unrelated means -- and every ``fetch``-only failure in
between (a network blip) does nothing to clear it, since :meth:`_checkout`
is never reached. :meth:`apply` therefore re-runs the same "clean tree
clears a stale marker" check unconditionally at its own start, before
``fetch`` -- so a marker can survive at most from one checkout attempt to
the very next ``apply()`` call, never longer, regardless of how many
unrelated failures happen in between.

**Residual accepted risk**, now narrowed to a single attempt's own live
window: a process killed *inside* one attempt, after this same attempt's
own marker-create (step 4) but before its checkout subprocess starts or
completes, leaves a marker and a dirty tree from that one attempt. If an
operator edits the tree in the narrow gap between that kill and the very
next ``apply()`` call reaching the dirty-tree guard, those edits are
indistinguishable from the attempt's own half-applied dirt and are lost to
the forced checkout. This is deliberately left as documented risk rather
than engineered away, since there is no channel for the Portal to observe
"an operator is about to touch this"; the change from earlier revisions of
this docstring is the size of the window -- it used to be able to persist
across arbitrarily many later apply attempts (fixed above), and now cannot
outlive the one attempt that created it.

**Stale git locks: a gated sweep, not an unconditional one.**
:meth:`_sweep_stale_locks` runs at the start of every ``apply``/rollback,
before ``fetch`` -- a killed ``git fetch``/``git checkout`` can leave
``.git/index.lock`` and friends behind, which makes every later ``git``
invocation in this checkout fail with "File exists" forever. But locks are
*not* always safe to delete on sight: an operator's own ``git`` command
running concurrently over SSH (a commit, a stash) holds a real, live lock,
and deleting it out from under that command can corrupt the operator's own
work -- and the moment right after a power cut, when an operator SSHes in
to investigate, is exactly when a live operator lock is most likely to
appear. A lock is removed only when one of two signals attributes it to
this updater:

- **The checkout marker exists, and the lock's mtime is no newer than the
  marker's.** A lock that already existed *before* this attempt's marker
  was even written can only be left over from something that predates
  whatever is currently attested -- never from an operator's command that
  starts *after* the marker (e.g. one run during a post-crash SSH rescue).
  A lock *newer* than the marker is ambiguous -- it could be this same
  crashed attempt's own lock, or an operator's brand-new one -- and falls
  through to the age-based signal below rather than getting automatic
  marker-based amnesty; this is a deliberate conservative trade against a
  proof-of-concept that showed the previous "marker present -> sweep
  everything" rule could delete a live operator lock created moments after
  a crash.
- **The lock has been observed, by this same running process, to persist
  for at least** :data:`_STALE_LOCK_MIN_AGE_SECONDS` **of its own**
  :func:`time.monotonic` **clock** -- not wall-clock age. See
  :data:`_STALE_LOCK_MIN_AGE_SECONDS`'s own comment for why wall-clock
  mtime is unsafe on an RTC-less Pi and what a reboot does to this signal.

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
3. *This marker mechanism itself, first revision.* A further adversarial
   review, with proofs-of-concept, found the mechanism's core (attest in
   the filesystem, at the moment of mutation, not from job history) sound,
   but four refinements were needed:

   - A stale marker on an already-clean tree could linger indefinitely
     (survives every intervening ``fetch``-only failure, since
     :meth:`_checkout` -- the only place that used to clear it -- is never
     reached) and license a force far later, against dirt an operator
     introduced long after the crash that created the marker. Fixed by
     re-running the "clean tree clears a stale marker" check
     unconditionally at the start of every :meth:`apply`, not only inside
     :meth:`_checkout` -- see "A clean tree at apply-start proves any
     marker stale" above.
   - The "marker present -> sweep every lock regardless of age" rule could
     delete a lock an operator's own concurrent ``git`` command was
     legitimately holding, precisely because a post-crash SSH rescue is
     exactly when an operator is likely to be running git by hand. Fixed
     by only trusting a lock the marker attests to when the lock's mtime
     does not postdate the marker's own -- see "Stale git locks" above.
   - The wall-clock age gate (locks older than 10 minutes by ``mtime``) is
     unsafe on an RTC-less Pi: an NTP step-forward can make a just-created,
     genuinely live lock appear hours old. Replaced with a
     per-process, :func:`time.monotonic`-based "observed to persist across
     this process's own attempts" gate -- see :data:`_STALE_LOCK_MIN_AGE_SECONDS`.
   - The marker's creation (:func:`~pathlib.Path.touch`) was not fsynced,
     so a power cut could lose the marker itself while the half-checkout
     dirt it was meant to attest to survived -- silently recreating the
     original wedge this class exists to fix. Fixed by writing it through
     :func:`~palmimo_portal.adapters.atomic_write.atomic_write_text`,
     which fsyncs the file and the parent (``.git``) directory before
     returning.
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

from palmimo_portal.adapters.atomic_write import atomic_write_text
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

#: Minimum time, in seconds of this *process's own* :func:`time.monotonic`
#: clock, a lock file must be observed to persist before
#: :meth:`GitUvUpdater._sweep_stale_locks` will remove it under the
#: age-based signal (the marker-mtime signal, when it applies, is
#: independent of this constant -- see the module docstring's "Stale git
#: locks"). Deliberately monotonic, not wall-clock ``mtime``: a Pi has no
#: RTC, and an NTP step-forward at boot can make a just-created, genuinely
#: live lock (an operator's concurrent ``git add`` over SSH) look hours
#: old by ``mtime`` alone. uv itself never takes a git lock -- this bound
#: is only about how long an interactive git command an operator might run
#: by hand could plausibly hold one. The trade this makes: after this
#: *process* restarts (a reboot, or the Portal's own systemd restart), the
#: monotonic clock -- and with it every lock's "first seen" bookkeeping --
#: restarts too, so a lock this updater's own earlier run left behind is
#: swept again only once this process has been up for this long, with
#: visible "File exists" ``fetch``/``checkout`` failures in the meantime.
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
    #: Test/production seam for the stale-lock sweep's age gate -- defaults
    #: to the real :func:`time.monotonic`. See :data:`_STALE_LOCK_MIN_AGE_SECONDS`.
    monotonic: Callable[[], float] = field(default=time.monotonic)
    _warned_not_git: bool = field(default=False, init=False, repr=False)
    _warned_not_plain_clone: bool = field(default=False, init=False, repr=False)
    #: In-memory "first observed at this monotonic time" bookkeeping for
    #: git lock files not (yet) attributable via the checkout marker --
    #: one :class:`GitUvUpdater` per running Portal process, so this
    #: naturally resets across a restart along with `monotonic()` itself.
    _lock_first_seen: dict[Path, float] = field(default_factory=dict, init=False, repr=False)

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
        self._clear_marker_if_tree_is_clean()
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

    def _clear_marker_if_tree_is_clean(self) -> None:
        """Clear a stale checkout marker at the start of every apply/rollback run.

        See the module docstring's "A clean tree at apply-start proves any
        marker stale" section: without this, a marker can otherwise outlive
        the dirt it was created for -- surviving any number of unrelated
        later ``fetch`` failures, since :meth:`_checkout` (the only other
        place that clears a stale marker) is never reached until ``fetch``
        and ``assets`` both succeed. Best-effort: if the dirty check itself
        cannot be run, the marker is left in place rather than treated as
        stale by default.
        """
        marker = self._checkout_marker_path()
        if marker is None or not marker.exists():
            return
        try:
            dirty = self._working_tree_is_dirty()
        except UpdateStepError:
            logger.warning(
                "could not check working-tree cleanliness while looking for a stale checkout marker -- "
                "leaving it in place"
            )
            return
        if not dirty:
            marker.unlink(missing_ok=True)
            logger.info("cleared a stale checkout-attestation marker: the working tree is already clean")

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
        # Written through atomic_write_text (fsync the file, fsync the
        # `.git` directory entry) so a power cut right after this cannot
        # lose the marker while the half-checkout dirt it attests to
        # survives.
        if marker is not None:
            atomic_write_text(marker, "")

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

        See the module docstring's "Stale git locks" section. Two
        independent signals attribute a lock to this updater:

        1. The checkout marker exists, and the lock's mtime is no newer
           than the marker's own -- it existed before this attempt's
           marker was even written, so it cannot be an operator's command
           that started afterward (e.g. during a post-crash SSH rescue).
        2. This process's own :attr:`monotonic` clock has observed the
           lock persist for at least :data:`_STALE_LOCK_MIN_AGE_SECONDS`,
           tracked per-lock in :attr:`_lock_first_seen` -- never by
           wall-clock ``mtime``, which an NTP step can misrepresent. A
           lock seen for the first time this call is recorded but never
           removed on that same call.

        A lock the marker attests to but whose mtime *postdates* the
        marker falls through to signal 2 instead of getting automatic
        marker-based amnesty -- see the module docstring for why.
        :attr:`_lock_first_seen` entries for locks that no longer exist are
        pruned first, so a lock that reappears at the same path later is
        treated as newly seen, not instantly eligible from stale
        bookkeeping.
        """
        git_dir = self._git_dir()
        if git_dir is None:
            return
        marker_path = git_dir / _CHECKOUT_MARKER_NAME
        try:
            marker_mtime: float | None = marker_path.stat().st_mtime
        except OSError:
            marker_mtime = None  # no marker (or a stat race) -- no marker-based amnesty this round

        candidates = [git_dir / name for name in _GIT_DIR_LOCK_NAMES]
        refs_dir = git_dir / "refs"
        if refs_dir.is_dir():
            candidates.extend(sorted(refs_dir.rglob("*.lock")))
        candidate_set = set(candidates)
        for tracked in list(self._lock_first_seen):
            if tracked not in candidate_set or not tracked.is_file():
                del self._lock_first_seen[tracked]

        now_monotonic = self.monotonic()
        removed: list[str] = []
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                attributable_via_marker = marker_mtime is not None and path.stat().st_mtime <= marker_mtime
                if attributable_via_marker:
                    attributable = True
                else:
                    first_seen = self._lock_first_seen.setdefault(path, now_monotonic)
                    attributable = (now_monotonic - first_seen) >= _STALE_LOCK_MIN_AGE_SECONDS
                if not attributable:
                    continue
                path.unlink()
                self._lock_first_seen.pop(path, None)
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
