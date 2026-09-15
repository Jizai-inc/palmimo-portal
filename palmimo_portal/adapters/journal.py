"""Real :class:`~palmimo_portal.ports.JournalPort`: ``journalctl -u <unit> -o json``.

No shell, no interpolated unit name in a shell string -- ``subprocess`` with
an argv list. ``-o json`` gives one JSON object per line, cheap to parse
without a journald client library dependency.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import subprocess

from palmimo_portal.ports import JournalEntry, JournalPage, JournalPort


logger = logging.getLogger("palmimo_portal")

_JOURNAL_GROUP = "systemd-journal"
_READ_TIMEOUT_SECONDS = 10.0


class JournalctlPort(JournalPort):
    def can_read(self) -> bool:
        try:
            journal_gid = grp.getgrnam(_JOURNAL_GROUP).gr_gid
        except KeyError:
            return False
        return journal_gid in os.getgroups()

    def read(self, unit: str, *, cursor: str | None, lines: int, invocation: str | None = None) -> JournalPage:
        args = ["journalctl", "-u", unit, "-o", "json", "--no-pager", "-n", str(lines)]
        if cursor is not None:
            args += ["--after-cursor", cursor]
        if invocation is not None:
            args += [f"_SYSTEMD_INVOCATION_ID={invocation}"]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=_READ_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.error("journal: journalctl failed for unit=%s: %s", unit, error)
            return JournalPage(entries=[], next_cursor=cursor, invocations=[])

        entries: list[JournalEntry] = []
        next_cursor = cursor
        invocations: list[str] = []
        seen_invocations: set[str] = set()
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp_us = record.get("__REALTIME_TIMESTAMP")
            invocation_id = record.get("_SYSTEMD_INVOCATION_ID")
            entries.append(
                JournalEntry(
                    message=str(record.get("MESSAGE", "")),
                    timestamp=(float(timestamp_us) / 1_000_000) if timestamp_us is not None else None,
                    invocation_id=invocation_id,
                )
            )
            if invocation_id is not None and invocation_id not in seen_invocations:
                seen_invocations.add(invocation_id)
                invocations.append(invocation_id)
            cursor_value = record.get("__CURSOR")
            if cursor_value is not None:
                next_cursor = cursor_value
        return JournalPage(entries=entries, next_cursor=next_cursor, invocations=invocations)
