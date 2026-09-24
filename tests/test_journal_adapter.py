"""Tests for :mod:`palmimo_portal.adapters.journal`.

``subprocess.run`` is monkeypatched to return scripted ``journalctl`` output, keyed off
whether the argv is the tail read (``-n <lines>``) or the invocation listing
(``--output-fields=...``) -- see the module docstring for why :meth:`JournalctlPort.read`
issues both.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from palmimo_portal.adapters.journal import JournalctlPort
from palmimo_portal.ports import JournalInvocation


def _journal_line(message: str, invocation_id: str, timestamp_us: int) -> str:
    return json.dumps(
        {"MESSAGE": message, "_SYSTEMD_INVOCATION_ID": invocation_id, "__REALTIME_TIMESTAMP": timestamp_us}
    )


def test_read_lists_a_previous_invocation_once_the_current_run_exceeds_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The old run has 3 lines, the current run has 5 -- a `-n 3` tail sees only the current run's
    # own lines, so listing invocations from that tail alone would make the old run unreachable.
    old_lines = [_journal_line(f"old-{i}", "inv-old", 1_000_000 + i) for i in range(3)]
    new_lines = [_journal_line(f"new-{i}", "inv-new", 2_000_000 + i) for i in range(5)]
    tail = new_lines[-3:]

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(arg.startswith("--output-fields=_SYSTEMD_INVOCATION_ID") for arg in args):
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(old_lines + new_lines), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="\n".join(tail), stderr="")

    monkeypatch.setattr("palmimo_portal.adapters.journal.subprocess.run", fake_run)

    page = JournalctlPort().read("palmimo-app@teleop.service", cursor=None, lines=3)

    assert [entry.message for entry in page.entries] == ["new-2", "new-3", "new-4"]
    assert page.invocations == [
        JournalInvocation(id="inv-new", started_at=2.0),
        JournalInvocation(id="inv-old", started_at=1.0),
    ]


def test_read_returns_no_invocations_when_the_listing_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(arg.startswith("--output-fields=_SYSTEMD_INVOCATION_ID") for arg in args):
            raise OSError("journalctl not found")
        return subprocess.CompletedProcess(args, 0, stdout=_journal_line("hello", "inv-1", 1_000_000), stderr="")

    monkeypatch.setattr("palmimo_portal.adapters.journal.subprocess.run", fake_run)

    page = JournalctlPort().read("palmimo-app@teleop.service", cursor=None, lines=10)

    assert [entry.message for entry in page.entries] == ["hello"]
    assert page.invocations == []


def test_read_includes_pid1s_own_line_about_the_unit_when_filtering_by_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PID1's "Started …"/"Main process exited …" lines about the unit carry INVOCATION_ID, not
    # _SYSTEMD_INVOCATION_ID (only the unit's own process sets that field) -- filtering with
    # `-u <unit>` ANDed onto _SYSTEMD_INVOCATION_ID alone would drop them.
    invocation_id = "a" * 32
    unit = "palmimo-app@teleop.service"
    pid1_line = json.dumps(
        {"MESSAGE": f"Started {unit}.", "UNIT": unit, "INVOCATION_ID": invocation_id, "__REALTIME_TIMESTAMP": 1_000_000}
    )
    app_line = json.dumps(
        {"MESSAGE": "app booted", "_SYSTEMD_INVOCATION_ID": invocation_id, "__REALTIME_TIMESTAMP": 1_000_100}
    )

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(arg.startswith("--output-fields=") for arg in args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        assert "-u" not in args, "the invocation id (128-bit, already unique) must scope the read alone"
        return subprocess.CompletedProcess(args, 0, stdout="\n".join([pid1_line, app_line]), stderr="")

    monkeypatch.setattr("palmimo_portal.adapters.journal.subprocess.run", fake_run)

    page = JournalctlPort().read(unit, cursor=None, lines=10, invocation=invocation_id)

    assert [entry.message for entry in page.entries] == [f"Started {unit}.", "app booted"]
    assert all(entry.invocation_id == invocation_id for entry in page.entries)


def test_list_invocations_includes_a_run_with_only_pid1s_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run that has just started has produced no output of its own yet -- only PID1's own
    # "Started …" line (INVOCATION_ID, no _SYSTEMD_INVOCATION_ID) exists for it.
    invocation_id = "b" * 32
    unit = "palmimo-app@teleop.service"
    pid1_line = json.dumps({"INVOCATION_ID": invocation_id, "__REALTIME_TIMESTAMP": 3_000_000})

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if any(arg.startswith("--output-fields=_SYSTEMD_INVOCATION_ID") for arg in args):
            return subprocess.CompletedProcess(args, 0, stdout=pid1_line, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("palmimo_portal.adapters.journal.subprocess.run", fake_run)

    page = JournalctlPort().read(unit, cursor=None, lines=10)

    assert page.invocations == [JournalInvocation(id=invocation_id, started_at=3.0)]
