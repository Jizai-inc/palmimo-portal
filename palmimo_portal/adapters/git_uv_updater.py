"""Real :class:`~palmimo_portal.ports.Updater`: ``fetch`` -> ``assets`` -> ``checkout`` -> ``sync`` -> ``install-assets``.

Applies one GitHub Release tag to the Portal checkout (this repository's
clone on the device) this process is itself running out of
(``settings.portal_dir``). ``fetch``/``checkout``/``sync`` shell out through
``runner`` (default :func:`subprocess.run`; faked by
``tests/test_git_uv_updater_adapter.py`` to assert argv/cwd/order).
``assets``/``install-assets`` download and unpack the frontend build from
the same GitHub Release via ``urllib`` (stdlib -- the device never runs
Node) through ``opener``, the same seam
:class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource` uses;
download/verify/extract/swap lives in
:mod:`palmimo_portal.adapters.static_asset`, shared with
:mod:`palmimo_portal.fetch_static` (the developer CLI equivalent).

Step order is ``fetch -> assets -> checkout -> sync -> install-assets``:
``assets`` stages and verifies the frontend tarball into
``static.tmp-<pid>`` (a sibling of ``static/``) *before* the working tree is
touched, so a 404/checksum/format failure leaves both the checkout and
``static/`` untouched. ``checkout`` (dirty-tree guard, tag verification,
``git checkout --detach``) only runs once a usable build is staged.
``install-assets`` swaps the staged directory into ``static/`` **last**,
after ``sync`` succeeds, so the running process keeps serving the old
``static/`` and old code until the swap and the subsequent
:class:`~palmimo_portal.core.update_runner.UpdateRunner` restart -- no
window where a half-updated backend serves a mismatched frontend.

If ``checkout`` or ``sync`` fails, the staging directory is removed (a retry
re-downloads) and the working tree is left where ``checkout`` put it;
``static/`` is untouched either way. Rollback reuses :meth:`GitUvUpdater.apply`
(``api/update.py`` picks the tag) and re-downloads that tag's own asset
rather than restoring a saved ``static/``.

**Checkout-attestation marker.** ``fetch``/``checkout`` run under
120s/60s timeouts that SIGKILL on expiry, and a power cut can hit either
with no warning; a killed ``checkout`` can leave a half-switched, dirty
working tree. The dirty-tree guard (``git status --porcelain
--untracked-files=no``, see :data:`DIRTY_TREE_REFUSAL_PREFIX`) exists to
stop the updater from touching a tree a *human* modified over SSH --
bypassing that refusal for genuinely USER-dirty state is never acceptable.
To tell "this process's own half-applied debris" apart from a human's edit
without trusting a possibly-stale job history, :meth:`_checkout` writes a
marker file at ``<portal_dir>/.git/palmimo-checkout-in-progress``
(outside the worktree) immediately before the one command that can mutate
the tree, ``git checkout``:

1. Dirty-tree guard runs. Dirty + marker present -> the dirt is this
   attempt's own attested debris; force through with ``git checkout
   --force --detach``. Dirty + no marker -> refuse
   (:data:`DIRTY_TREE_REFUSAL_PREFIX`), unconditionally.
2. Clean tree -> clear any stale marker (nothing to repair) and resolve
   the tag. This same clear-if-clean check also runs unconditionally at
   the very start of :meth:`apply`, before ``fetch`` -- a marker can
   otherwise outlive the dirt it was created for and survive any number
   of unrelated ``fetch``-only failures in between, since :meth:`_checkout`
   is the only other place that clears it.
3. Resolve ``refs/tags/<tag>^{commit}``. On failure the tree was never
   touched this attempt, so no marker is created -- a later retry cannot
   force through a tree an operator dirties in the meantime.
4. Only now, immediately before ``git checkout`` itself, create the
   marker (a plain non-forced checkout also creates it, since it can still
   leave the tree dirty via a mid-write kill). Written through
   :func:`~palmimo_portal.adapters.atomic_write.atomic_write_text`, which
   fsyncs the file and the ``.git`` directory entry, so a power cut right
   after cannot lose the marker while the dirt it attests to survives.
5. Run ``git checkout``. Success -> remove the marker. Failure/timeout ->
   re-check dirtiness: still clean -> remove the marker (nothing was
   applied; a retry hits the same visible error until an operator
   resolves it, never silently forced); dirty -> keep the marker for the
   next attempt to force through; recheck itself fails -> keep the marker
   (a forced checkout of an actually-clean tree is a no-op, while
   discarding it risks re-wedging the device).

A ``.git`` that is not a plain directory (submodule/worktree layout) makes
the marker and the lock sweep below silent no-ops, logged once at WARNING
-- the device deploy contract is a plain ``git clone``.

**Residual accepted risk:** a process killed *inside* one attempt, after
that attempt's own marker-create (step 4) but before its ``git checkout``
subprocess starts or completes, leaves a marker and a dirty tree from that
one attempt. If an operator edits the tree in the narrow gap between that
kill and the next ``apply()`` call's dirty-tree guard, those edits are
indistinguishable from the attempt's own debris and are lost to the forced
checkout. Left as documented risk, not engineered away -- there is no
channel for the Portal to observe "an operator is about to touch this" --
but bounded to at most one attempt's live window by the apply-start clear
in step 2.

**Stale git locks: gated, not unconditional.** :meth:`_sweep_stale_locks`
runs at the start of every ``apply``/rollback, before ``fetch``, because a
killed ``git fetch``/``checkout`` can leave ``.git/index.lock`` and
friends behind, breaking every later ``git`` call with "File exists"
forever. Locks are not always safe to delete on sight -- an operator's own
concurrent ``git`` command over SSH holds a real lock, and a post-crash
SSH rescue is exactly when that is likely. A lock is removed only when:

- the checkout marker exists and the lock's mtime is no newer than the
  marker's (so it predates this attempt and cannot be an operator's
  command that started after the marker); a lock *newer* than the marker
  is ambiguous and falls through to the next signal instead of getting
  automatic amnesty; or
- this process's own :func:`time.monotonic` clock has observed the lock
  persist for at least :data:`_STALE_LOCK_MIN_AGE_SECONDS` -- not
  wall-clock age (see that constant's own comment for why mtime is unsafe
  on an RTC-less Pi).

A lock matching neither is left alone; the git call that needs it fails
visibly, and a retry succeeds once the lock is genuinely stale or the
operator's command has finished. One WARNING logs what was actually
removed.
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

#: Stable prefix of the dirty-tree guard's refusal message. Named so a test
#: can pin the wording without duplicating it at each raise site.
DIRTY_TREE_REFUSAL_PREFIX = "working tree has local changes"

#: Checkout-attestation marker filename, under ``.git/``. See module docstring.
_CHECKOUT_MARKER_NAME = "palmimo-checkout-in-progress"

#: Minimum time, in seconds of this process's own :func:`time.monotonic`
#: clock, a lock must be observed to persist before the age-based sweep
#: signal removes it (module docstring's "Stale git locks"). Deliberately
#: monotonic, not wall-clock ``mtime``: a Pi has no RTC, and an NTP
#: step-forward at boot can make a just-created, genuinely live lock (an
#: operator's concurrent ``git`` command over SSH) look hours old by
#: ``mtime`` alone. Restarting this process (reboot, systemd restart) also
#: restarts the monotonic clock and the "first seen" bookkeeping, so a lock
#: left by this updater's own earlier run is swept only after this process
#: has been up this long, with visible "File exists" failures until then.
_STALE_LOCK_MIN_AGE_SECONDS = 600.0

#: Git lock files under ``.git/`` a killed ``fetch``/``checkout`` can leave
#: behind. See :meth:`GitUvUpdater._sweep_stale_locks`.
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
    #: Release repo the ``assets`` step downloads the tarball from, same as
    #: :class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource`.
    update_repo: str = "Jizai-inc/palmimo-portal"
    opener: Opener = field(default=default_opener)
    #: Test seam for the stale-lock sweep's age gate; see :data:`_STALE_LOCK_MIN_AGE_SECONDS`.
    monotonic: Callable[[], float] = field(default=time.monotonic)
    _warned_not_git: bool = field(default=False, init=False, repr=False)
    _warned_not_plain_clone: bool = field(default=False, init=False, repr=False)
    #: Per-lock "first observed" monotonic time, for locks not attributable
    #: via the marker. Resets on process restart along with `monotonic()`.
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

    def _git_dir(self) -> Path | None:
        """Return ``.git``, or ``None`` (logging once) when it is not a plain directory. See module docstring."""
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
        """Clear a stale checkout marker at the start of every apply/rollback run. See module docstring.

        Best-effort: if the dirty check cannot be run, the marker is left in place.
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
        """Decide, after a checkout failure, whether the marker still attests real damage. See module docstring step 5.

        Never raises -- runs from an except block about to re-raise the original failure.
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
        """Refuse a dirty checkout unless it is attested updater debris, then check the tag out. See module docstring."""
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

    def _sweep_stale_locks(self) -> None:
        """Remove git lock files this updater can attribute to itself. See module docstring 'Stale git locks'.

        `_lock_first_seen` entries for locks that no longer exist are pruned first, so a
        lock reappearing at the same path later is treated as newly seen.
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
        """Download, verify, and stage the frontend Release asset. See module docstring.

        Returns the staging directory (not yet swapped into ``static/``); the caller
        removes it if a later step fails.
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
