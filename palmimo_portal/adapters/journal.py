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

from palmimo_portal.ports import JournalEntry, JournalInvocation, JournalPage, JournalPort


logger = logging.getLogger("palmimo_portal")

_JOURNAL_GROUP = "systemd-journal"
_READ_TIMEOUT_SECONDS = 10.0

#: Most recent invocation ids offered on "view a previous start" -- design doc 3.2 needs a short,
#: bounded list, not every start the unit has ever had.
_INVOCATION_LIST_LIMIT = 20


class JournalctlPort(JournalPort):
    def can_read(self) -> bool:
        try:
            journal_gid = grp.getgrnam(_JOURNAL_GROUP).gr_gid
        except KeyError:
            return False
        return journal_gid in os.getgroups()

    def read(self, unit: str, *, cursor: str | None, lines: int, invocation: str | None = None) -> JournalPage:
        args = ["journalctl", "-o", "json", "--no-pager", "-n", str(lines)]
        if invocation is not None:
            # PID1's own lines about this unit ("Started …", "Main process exited …") carry
            # _PID=1, UNIT=<unit>, and INVOCATION_ID -- never _SYSTEMD_INVOCATION_ID (only the
            # unit's own process sets that field) and never _SYSTEMD_UNIT (that identifies PID1's
            # own unit, init.scope, not the unit it is logging about). Two AND-groups joined by
            # `+` (journalctl's OR), one per line shape, each still scoped to this unit -- an
            # invocation id alone is unique, but not scoping by unit would let a request for one
            # app's invocation id read another unit's journal if that id ever collided or was
            # guessed.
            args += [
                f"_SYSTEMD_UNIT={unit}",
                f"_SYSTEMD_INVOCATION_ID={invocation}",
                "+",
                "_PID=1",
                f"UNIT={unit}",
                f"INVOCATION_ID={invocation}",
            ]
        else:
            args += ["-u", unit]
        if cursor is not None:
            args += ["--after-cursor", cursor]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=_READ_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.error("journal: journalctl failed for unit=%s: %s", unit, error)
            return JournalPage(entries=[], next_cursor=cursor, invocations=[])

        entries: list[JournalEntry] = []
        next_cursor = cursor
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp_us = record.get("__REALTIME_TIMESTAMP")
            invocation_id = record.get("_SYSTEMD_INVOCATION_ID") or record.get("INVOCATION_ID")
            entries.append(
                JournalEntry(
                    message=str(record.get("MESSAGE", "")),
                    timestamp=(float(timestamp_us) / 1_000_000) if timestamp_us is not None else None,
                    invocation_id=invocation_id,
                )
            )
            cursor_value = record.get("__CURSOR")
            if cursor_value is not None:
                next_cursor = cursor_value
        return JournalPage(entries=entries, next_cursor=next_cursor, invocations=self._list_invocations(unit))

    def _list_invocations(self, unit: str) -> list[JournalInvocation]:
        """Return up to :data:`_INVOCATION_LIST_LIMIT` invocation starts for ``unit``, newest first.

        A separate, field-restricted ``journalctl`` call over the unit's whole journal --
        :meth:`read`'s own ``-n lines`` tail only ever sees the *current* run once it has produced
        more than ``lines`` entries, which would otherwise make an earlier run permanently
        unreachable from "view a previous start" once the current one grows past the page size.

        Ordered by appearance in the journal stream, not by ``started_at``: a Pi with no RTC can
        report a wall-clock time for a fresh boot that sorts before an earlier run's real time,
        which would otherwise put that run in the wrong place (or hide it) until NTP catches up.
        """
        args = [
            "journalctl",
            "-u",
            unit,
            "-o",
            "json",
            "--no-pager",
            "-q",
            "--output-fields=_SYSTEMD_INVOCATION_ID,INVOCATION_ID,__REALTIME_TIMESTAMP",
        ]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=_READ_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.error("journal: journalctl invocation listing failed for unit=%s: %s", unit, error)
            return []
        invocations: dict[str, float | None] = {}
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # PID1's "Started <unit>" line (INVOCATION_ID) is the first line of a run and lands
            # before the unit's own process has logged anything (_SYSTEMD_INVOCATION_ID) -- a run
            # with no output of its own yet would otherwise be invisible here.
            invocation_id = record.get("_SYSTEMD_INVOCATION_ID") or record.get("INVOCATION_ID")
            if invocation_id is not None and invocation_id not in invocations:
                timestamp_us = record.get("__REALTIME_TIMESTAMP")
                invocations[invocation_id] = (float(timestamp_us) / 1_000_000) if timestamp_us is not None else None
        starts = [JournalInvocation(id=id, started_at=started_at) for id, started_at in invocations.items()]
        starts.reverse()
        return starts[:_INVOCATION_LIST_LIMIT]
