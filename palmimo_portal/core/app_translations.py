"""Optional display text; invalid translations never change app execution."""

from __future__ import annotations

import json
import re
from typing import Any


MAX_TRANSLATIONS_BYTES = 65_536
_LOCALE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
_KEY = re.compile(r"^(?:description|env\.[A-Z][A-Z0-9_]{0,63}|params\.[a-z][a-z0-9_]{0,31})$")


def parse_translations(raw: Any) -> dict[str, dict[str, str]]:
    """Keep bounded, valid display strings, falling back per missing field."""
    if not isinstance(raw, dict) or len(raw) > 32:
        return {}
    result: dict[str, dict[str, str]] = {}
    for locale, messages in raw.items():
        if not isinstance(locale, str) or not _LOCALE.fullmatch(locale) or not isinstance(messages, dict):
            continue
        result[locale] = {
            key: value
            for key, value in messages.items()
            if isinstance(key, str)
            and _KEY.fullmatch(key)
            and isinstance(value, str)
            and 1 <= len(value.strip()) <= 200
        }
    return result


def parse_translation_file(text: str) -> dict[str, dict[str, str]]:
    """Read a schema-1 companion file without accepting executable settings."""
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1:
        return {}
    return parse_translations(raw.get("translations"))
