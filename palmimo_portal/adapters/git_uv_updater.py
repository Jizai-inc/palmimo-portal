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
   frontend build exists for this tag.
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

**Being killed mid-``fetch``/``checkout``.** The step timeouts SIGKILL a
stuck subprocess, and a power cut does the same without even that much
grace. Either can leave ``.git/index.lock`` and friends behind (every later
git call then fails "File exists") and/or a half-checked-out dirty working
tree. :meth:`apply` sweeps stale git lock files unconditionally at the
start of every run (:meth:`_sweep_stale_locks`) -- they can only be debris
from a previous run of this same updater, never a live process, since git
is always run synchronously one step at a time. That sweep runs for
``fetch`` too, even though ``fetch`` cannot dirty the tree itself (see
below) -- a killed ``fetch`` can still leave lock files.

The dirty-tree guard itself stays in place for a USER-dirty tree (local
changes an operator made over SSH) -- that remains an accepted risk
requiring manual resolution, unchanged. What no longer requires SSH is a
tree the *updater itself* left dirty mid-``checkout``: when the caller
passes ``repair_dirty=True`` -- because
``core/update.should_repair_dirty_checkout`` found the previous job in
``update.json`` failed at ``"checkout"`` for a reason *other than* the
guard's own refusal (a timeout, a nonzero git exit, or
``finalize_after_restart``'s "interrupted" resolution of a process that
died mid-step) -- the guard is skipped and ``checkout`` runs ``--force``,
clobbering the interrupted half-checkout with the validated tag rather
than refusing it forever.

Two cases are deliberately *not* repairable, both to keep the guard's
accepted-risk contract intact:

- **A failed ``"fetch"``** never repairs, even though it is a git step
  that can fail: ``fetch`` does not touch the working tree at all, so it
  cannot be the reason a later ``checkout`` finds the tree dirty. Treating
  it as repairable would add risk for no benefit -- "GitHub was
  unreachable at fetch, and the tree happens to be dirty from an
  operator's SSH edit" would then also enable the force-clobber on the
  next apply.
- **The dirty-tree refusal itself** never repairs. It is recorded as a
  ``"checkout"`` failure (``fetch``/``assets`` already succeeded by the
  time it raises) with a message built from
  ``core.update.DIRTY_TREE_REFUSAL_PREFIX`` -- the same constant
  :func:`~palmimo_portal.core.update.should_repair_dirty_checkout` checks
  for, so the two sides cannot drift apart. Without this exclusion, a
  USER-dirty tree would be refused on the first apply attempt, recorded
  exactly like a killed checkout would be, and then force-clobbered by the
  very next apply -- one retry away from silently destroying an operator's
  changes.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
from palmimo_portal.core.update import DIRTY_TREE_REFUSAL_PREFIX, STATIC_ASSET_NAME_TEMPLATE
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

#: Git lock files directly under ``.git/`` that a killed ``git fetch``/
#: ``git checkout`` can leave behind -- see :meth:`GitUvUpdater._sweep_stale_locks`.
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

    def apply(self, tag: str, on_step: Callable[[str], None], *, repair_dirty: bool = False) -> None:
        self._sweep_stale_locks()
        self._step("fetch", ["git", "fetch", "--tags", "origin"], FETCH_TIMEOUT_SECONDS, on_step)
        temp_dir = self._assets(tag, on_step)
        try:
            self._checkout(tag, on_step, repair_dirty=repair_dirty)
            self._sync(on_step)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        self._install_assets(temp_dir, on_step)

    def _sweep_stale_locks(self) -> None:
        """Remove stale git lock files under ``.git/`` before every apply/rollback run.

        A killed ``git fetch``/``git checkout`` (the step timeouts SIGKILL
        the subprocess; a power cut does the same without even that much
        grace) can leave ``.git/index.lock`` and friends behind, which
        makes every later git invocation in this checkout fail with "File
        exists" forever -- this device could otherwise never update again
        without an operator clearing it over SSH.

        Unconditional at the start of every run, not conditioned on
        ``repair_dirty``: this class runs git synchronously, one
        subprocess at a time (see the module docstring), so a lock file
        found here cannot belong to a live git process of this updater's
        own -- it can only be debris from a previous run of *this same
        updater* (this method's caller), which is exactly the "messes it
        can attribute to itself" the module docstring's accepted-risk
        sentence describes. Logs one WARNING naming what was removed;
        silent when there is nothing to remove.
        """
        git_dir = self.portal_dir / ".git"
        candidates = [git_dir / name for name in _GIT_DIR_LOCK_NAMES]
        refs_dir = git_dir / "refs"
        if refs_dir.is_dir():
            candidates.extend(sorted(refs_dir.rglob("*.lock")))
        removed: list[str] = []
        for path in candidates:
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path.relative_to(self.portal_dir)))
            except OSError as error:
                logger.warning("could not remove stale git lock %s: %s", path, error)
        if removed:
            logger.warning("removed stale git lock file(s) before update: %s", ", ".join(removed))

    def _checkout(self, tag: str, on_step: Callable[[str], None], *, repair_dirty: bool = False) -> None:
        """Refuse a dirty checkout, resolve ``tag`` against a fully-qualified ref, then check it out.

        Three subprocess calls under one ``on_step("checkout")`` -- a caller
        watching progress only needs "we are switching versions", not which
        git invocation is running.

        1. ``git status --porcelain --untracked-files=no`` -- refuses to
           touch a checkout with local changes; an operator resolves that
           over SSH first (commit, stash, or reset). Skipped entirely when
           ``repair_dirty`` is ``True``: the caller (``core/update.py``'s
           :func:`~palmimo_portal.core.update.should_repair_dirty_checkout`)
           has already determined the *updater's own previous job* died
           mid-``"checkout"`` for a reason other than this very refusal
           (a timeout, a nonzero git exit, or an interrupted-restart
           resolution), so any dirt here can only be that job's
           half-finished work, not a human's.
        2. ``git rev-parse --verify --quiet refs/tags/<tag>^{commit}`` --
           confirms the tag exists after the fetch above, resolved against
           the fully-qualified ``refs/tags/`` ref so this can't be tricked
           into resolving a same-named branch instead.
        3. ``git checkout --detach refs/tags/<tag>`` -- same fully-qualified
           ref, same reason. When ``repair_dirty`` is ``True`` this becomes
           ``git checkout --force --detach refs/tags/<tag>``, which clobbers
           the interrupted half-checkout rather than refusing it.
        """
        on_step("checkout")
        if not repair_dirty:
            status = self._run_or_raise(
                "checkout", ["git", "status", "--porcelain", "--untracked-files=no"], CHECKOUT_TIMEOUT_SECONDS
            )
            if status.returncode != 0:
                raise UpdateStepError(
                    "checkout", _stderr_tail(status.stderr or "") or f"git status exited {status.returncode}"
                )
            if status.stdout.strip():
                raise UpdateStepError(
                    "checkout",
                    f"{DIRTY_TREE_REFUSAL_PREFIX}; commit, stash, or reset them over SSH before updating",
                )

        ref = f"refs/tags/{tag}"
        verify = self._run_or_raise(
            "checkout", ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], CHECKOUT_TIMEOUT_SECONDS
        )
        if verify.returncode != 0:
            raise UpdateStepError("checkout", "tag not found after fetch")

        checkout_argv = ["git", "checkout", "--detach", ref]
        if repair_dirty:
            checkout_argv.insert(2, "--force")
        checkout = self._run_or_raise("checkout", checkout_argv, CHECKOUT_TIMEOUT_SECONDS)
        if checkout.returncode != 0:
            raise UpdateStepError(
                "checkout", _stderr_tail(checkout.stderr or "") or f"checkout {ref} exited {checkout.returncode}"
            )

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
