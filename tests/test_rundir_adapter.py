"""The run directory's modes are fixed by the adapter, not by the process umask."""

import json
import os
import stat
from pathlib import Path

import pytest

from palmimo_portal.adapters.rundir import TmpfsRunDirPort


#: systemd's own character classes (src/fundamental/string-util.h) and the set that needs
#: backslash-unescaping inside a double-quoted value (src/basic/escape.h, SHELL_NEED_ESCAPE),
#: fetched from github.com/systemd/systemd -- exact strings, not recalled from memory.
_NEWLINE = "\n\r"
_WHITESPACE = " \t\n\r"
_SHELL_NEED_ESCAPE = '"\\`$'


def _parse_systemd_env_file(text: str) -> dict[str, str]:
    """Independent port of systemd's ``parse_env_file_internal`` double-quoted-value path
    (``src/basic/env-file.c``, ``EnvironmentFile=``'s own parser -- see systemd.exec(5)).

    Not derived from :func:`~palmimo_portal.adapters.rundir._quote_env_value`: it exists as an
    oracle independent of the code under test, so a bug in that function's escaping shows up as
    a mismatch here rather than being invisible because both sides share the same logic. Only
    the states the adapter's own quoting can produce are ported (double-quoted, single-line
    values); unquoted-value whitespace trimming and comment lines are out of scope because the
    adapter never writes them.
    """
    result: dict[str, str] = {}
    state = "PRE_KEY"
    key_chars: list[str] = []
    value_chars: list[str] = []

    def push() -> None:
        result["".join(key_chars)] = "".join(value_chars)

    for c in text:
        if state == "PRE_KEY":
            if c not in _WHITESPACE:
                state = "KEY"
                key_chars = [c]
        elif state == "KEY":
            if c in _NEWLINE:
                state, key_chars = "PRE_KEY", []
            elif c == "=":
                state, value_chars = "PRE_VALUE", []
            else:
                key_chars.append(c)
        elif state == "PRE_VALUE":
            if c in _NEWLINE:
                push()
                state, key_chars = "PRE_KEY", []
            elif c == '"':
                state = "DOUBLE_QUOTE_VALUE"
            elif c == "'":
                state = "SINGLE_QUOTE_VALUE"
            elif c == "\\":
                state = "VALUE_ESCAPE"
            elif c not in _WHITESPACE:
                state = "VALUE"
                value_chars.append(c)
        elif state == "VALUE":
            if c in _NEWLINE:
                push()
                state, key_chars = "PRE_KEY", []
            elif c == "\\":
                state = "VALUE_ESCAPE"
            else:
                value_chars.append(c)
        elif state == "VALUE_ESCAPE":
            state = "VALUE"
            if c not in _NEWLINE:
                value_chars.append(c)
        elif state == "SINGLE_QUOTE_VALUE":
            if c == "'":
                state = "PRE_VALUE"
            else:
                value_chars.append(c)
        elif state == "DOUBLE_QUOTE_VALUE":
            if c == '"':
                state = "PRE_VALUE"
            elif c == "\\":
                state = "DOUBLE_QUOTE_VALUE_ESCAPE"
            else:
                value_chars.append(c)
        elif state == "DOUBLE_QUOTE_VALUE_ESCAPE":
            state = "DOUBLE_QUOTE_VALUE"
            if c in _SHELL_NEED_ESCAPE:
                value_chars.append(c)
            elif c != "\n":
                value_chars.extend(("\\", c))
    if key_chars:
        push()
    return result


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_write_gives_the_app_directory_and_argv_group_readable_modes_under_any_umask(
    tmp_path: Path, umask: int
) -> None:
    # Another thread can hold a restrictive umask while this runs; the app account reads
    # argv.json through the directory, so both modes must not depend on it.
    previous = os.umask(umask)
    try:
        TmpfsRunDirPort(tmp_path).write("app", env={"A": "1"}, argv=["x"], cwd="/tmp", project="/tmp", python=">=3.12")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((tmp_path / "app").stat().st_mode) == 0o750
    assert stat.S_IMODE((tmp_path / "app" / "argv.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "app" / "env").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "app" / "argv.json").read_text()) == {
        "argv": ["x"],
        "cwd": "/tmp",
        "project": "/tmp",
        "python": ">=3.12",
    }


@pytest.mark.parametrize(
    "value",
    [
        'has "double quotes" inside',
        "has\\backslashes\\in\\it",
        "  leading and trailing whitespace  ",
        "has a $DOLLAR and a `backtick`",
        '"\\ a value that is $everything at \\" once`  ',
    ],
    ids=["double_quote", "backslash", "surrounding_whitespace", "dollar_and_backtick", "mixed"],
)
def test_write_env_value_round_trips_through_systemds_environmentfile_parser(tmp_path: Path, value: str) -> None:
    # Without quoting, systemd's EnvironmentFile= parser trims unquoted leading/trailing
    # whitespace and treats a bare "\" or "\"" as parse-breaking syntax -- an app whose env value
    # needs any of these (a token with a trailing space, a Windows-style path, a value that
    # happens to contain a quote) would silently receive a corrupted value at launch.
    TmpfsRunDirPort(tmp_path).write("app", env={"V": value}, argv=["x"], cwd="/tmp", project="/tmp")

    written = (tmp_path / "app" / "env").read_text(encoding="utf-8")

    assert _parse_systemd_env_file(written) == {"V": value}
