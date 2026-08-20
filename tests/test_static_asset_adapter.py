"""Tests for :mod:`palmimo_portal.adapters.static_asset`'s ``swap_into_place``/``repair_static_dir``.

Both cover failure modes a killed process can leave behind around
``static/``: :func:`~palmimo_portal.adapters.static_asset.swap_into_place`'s
own best-effort restore when the *second* rename fails, and
:func:`~palmimo_portal.adapters.static_asset.repair_static_dir`'s boot-time
repair when the process dies *between* the two renames (so neither of
``swap_into_place``'s own ``try``/``except`` branches ever runs at all).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from palmimo_portal.adapters.static_asset import StaticAssetError, repair_static_dir, swap_into_place


def _make_dir(path: Path, *, marker: str) -> None:
    path.mkdir(parents=True)
    (path / "index.html").write_text(marker, encoding="utf-8")


def _dead_pid() -> int:
    """Return a pid that is provably dead: a just-spawned, already-``wait()``-ed subprocess.

    ``subprocess.Popen().wait()`` guarantees the child has exited (and, on
    POSIX, been reaped) by the time it returns, so ``os.kill(pid, 0)``
    reliably raises :class:`ProcessLookupError` for it afterward -- the
    simplest portable way to get a pid :func:`repair_static_dir`'s liveness
    check will treat as dead, without relying on a hardcoded pid number
    that might coincidentally belong to a real long-lived process (e.g.
    ``1``, the init/launchd process, which is always alive).
    """
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=5.0)
    return process.pid


def test_swap_into_place_restores_static_prev_when_the_second_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static_dir = tmp_path / "static"
    temp_dir = tmp_path / "static.tmp-123"
    _make_dir(static_dir, marker="old build")
    _make_dir(temp_dir, marker="new build")

    original_rename = Path.rename
    call_count = 0

    def flaky_rename(self: Path, target: Path) -> None:
        nonlocal call_count
        call_count += 1
        # The first rename (static/ -> static.prev) is swap_into_place's own
        # backup step -- let it succeed. The second (temp_dir -> static/) is
        # the one that actually fails here.
        if call_count == 2:
            raise OSError("simulated rename failure")
        original_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(StaticAssetError):
        swap_into_place(temp_dir, static_dir)

    # static/ was never left missing -- the old build was restored.
    assert static_dir.is_dir()
    assert (static_dir / "index.html").read_text(encoding="utf-8") == "old build"


def test_repair_static_dir_restores_static_from_static_prev_when_static_is_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    static_dir = tmp_path / "static"
    prev_dir = tmp_path / "static.prev"
    _make_dir(prev_dir, marker="last known good build")
    assert not static_dir.exists()

    with caplog.at_level(logging.WARNING):
        repair_static_dir(static_dir)

    assert static_dir.is_dir()
    assert (static_dir / "index.html").read_text(encoding="utf-8") == "last known good build"
    assert not prev_dir.exists()
    assert "restored the previous frontend build from" in caplog.text
    assert "install-assets" in caplog.text


def test_repair_static_dir_is_a_no_op_when_static_exists(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    prev_dir = tmp_path / "static.prev"
    _make_dir(static_dir, marker="current build")
    _make_dir(prev_dir, marker="stale backup")  # e.g. a killed swap that never removed it

    repair_static_dir(static_dir)

    # static/ untouched -- static.prev is left for the tmp-sweep half below,
    # or a future update's own swap_into_place, to deal with; this function
    # only ever repairs a *missing* static/.
    assert (static_dir / "index.html").read_text(encoding="utf-8") == "current build"


def test_repair_static_dir_is_a_no_op_when_neither_static_nor_prev_exists(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"  # a source checkout that never ran `make build`

    repair_static_dir(static_dir)  # must not raise

    assert not static_dir.exists()


def test_repair_static_dir_removes_orphaned_tmp_siblings(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    static_dir = tmp_path / "static"
    _make_dir(static_dir, marker="current build")
    orphan = tmp_path / f"static.tmp-{_dead_pid()}"
    _make_dir(orphan, marker="half-staged build from a killed process")

    with caplog.at_level(logging.INFO):
        repair_static_dir(static_dir)

    assert not orphan.exists()
    assert "removed orphaned update staging directory" in caplog.text


def test_repair_static_dir_removes_multiple_orphaned_tmp_siblings(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    _make_dir(static_dir, marker="current build")
    _make_dir(tmp_path / f"static.tmp-{_dead_pid()}", marker="a")
    _make_dir(tmp_path / f"static.tmp-{_dead_pid()}", marker="b")

    repair_static_dir(static_dir)

    assert list(tmp_path.glob("static.tmp-*")) == []


def test_repair_static_dir_does_not_delete_a_staging_dir_owned_by_a_live_pid(tmp_path: Path) -> None:
    # os.getpid() -- this test process itself -- is guaranteed alive for
    # the duration of the test, so a static.tmp-<pid> named after it must
    # survive repair: a second, hand-started repair process must not
    # delete a staging directory a live process is still writing to.
    static_dir = tmp_path / "static"
    _make_dir(static_dir, marker="current build")
    live = tmp_path / f"static.tmp-{os.getpid()}"
    _make_dir(live, marker="still being staged by a live process")

    repair_static_dir(static_dir)

    assert live.is_dir()


def test_repair_static_dir_deletes_a_staging_dir_owned_by_a_dead_pid(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    _make_dir(static_dir, marker="current build")
    dead = tmp_path / f"static.tmp-{_dead_pid()}"
    _make_dir(dead, marker="left behind by a process that died mid-stage")

    repair_static_dir(static_dir)

    assert not dead.exists()


def test_repair_static_dir_deletes_a_staging_dir_with_a_malformed_pid_suffix(tmp_path: Path) -> None:
    # A directory name that does not parse as static.tmp-<int> cannot
    # belong to any process this repair could confirm alive -- treated the
    # same as a confirmed-dead one and cleaned up.
    static_dir = tmp_path / "static"
    _make_dir(static_dir, marker="current build")
    malformed = tmp_path / "static.tmp-not-a-pid"
    _make_dir(malformed, marker="garbage suffix")

    repair_static_dir(static_dir)

    assert not malformed.exists()


def test_repair_static_dir_both_restores_and_sweeps_in_one_call(tmp_path: Path) -> None:
    static_dir = tmp_path / "static"
    prev_dir = tmp_path / "static.prev"
    _make_dir(prev_dir, marker="last known good build")
    _make_dir(tmp_path / f"static.tmp-{_dead_pid()}", marker="the build that was mid-swap when the process died")

    repair_static_dir(static_dir)

    assert (static_dir / "index.html").read_text(encoding="utf-8") == "last known good build"
    assert list(tmp_path.glob("static.tmp-*")) == []
