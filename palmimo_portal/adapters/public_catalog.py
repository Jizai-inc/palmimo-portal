from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from palmimo_portal.adapters.static_asset import Opener, StaticAssetError, default_opener, download
from palmimo_portal.core.app_translations import parse_translations
from palmimo_portal.ports import (
    AppSource,
    CatalogEnvSpec,
    PublicAppIdentity,
    PublicCatalogApp,
    PublicCatalogAsset,
    PublicCatalogSource,
    PublicCatalogSourceError,
)
from palmimo_portal.settings import DEFAULT_PUBLIC_CATALOG_URL
from palmimo_portal.version import portal_version


logger = logging.getLogger("palmimo_portal")
MAX_CATALOG_BYTES = 5 * 1024 * 1024
MAX_CATALOG_APPS = 2000
_NAME = r"[a-z][a-z0-9-]{0,39}"
_NAMESPACE = r"[a-z0-9][a-z0-9-]{0,39}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_URL = r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}"
_REF = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}"
_TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"


def _matches(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _help_url(value: Any) -> bool:
    if not isinstance(value, str) or not re.match(r"https?://", value, re.IGNORECASE):
        return False
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
        return bool(parsed.hostname) and not any(char.isspace() for char in parsed.netloc)
    except ValueError:
        return False


def _parse_item(raw: Any, index: int) -> PublicCatalogApp:
    item = _object(raw)
    public = _object(item["public"])
    source = _object(item["source"])
    if not (
        _matches(item["name"], _NAME)
        and isinstance(item["description"], str)
        and _matches(public["app_id"], _UUID)
        and _matches(public["namespace"], _NAMESPACE)
        and type(public["official"]) is bool
        and source["type"] == "git"
        and _matches(source["url"], _URL)
        and not source["url"].endswith(".git")
        and _matches(source["ref"], _REF)
        and source["ref_kind"] in ("tag", "branch")
        and _matches(source["commit"], r"[0-9a-f]{40}")
    ):
        raise ValueError("invalid identity or source")
    subdir = source["subdir"]
    if subdir is not None and (
        not isinstance(subdir, str)
        or not subdir
        or subdir != subdir.strip()
        or subdir.startswith("/")
        or subdir.endswith("/")
        or ".." in subdir.split("/")
        or "\\" in subdir
        or any(ord(char) < 32 or ord(char) == 127 for char in subdir)
    ):
        raise ValueError("invalid subdir")
    manifest = source["manifest"]
    if manifest is not None and not _matches(manifest, rf"palmimo\.{_NAME}\.toml"):
        raise ValueError("invalid manifest")
    devices = item["devices"]
    if not isinstance(devices, list) or any(device not in ("camera", "audio", "motor_display") for device in devices):
        raise ValueError("invalid devices")
    env: dict[str, CatalogEnvSpec] = {}
    for name, raw_spec in _object(item["env"]).items():
        spec = _object(raw_spec)
        if type(spec["required"]) is not bool or not isinstance(spec["description"], str):
            raise ValueError("invalid env")
        help_url = spec.get("help_url")
        if "help_url" in spec and not _help_url(help_url):
            logger.warning("public catalog item %s: dropped env.%s.help_url", index, name)
            help_url = None
        env[name] = CatalogEnvSpec(spec["required"], spec["description"], help_url)
    namespace = public["namespace"]
    official = public["official"]
    if namespace in ("zip", "git", "cloud"):
        raise ValueError("reserved namespace")
    if namespace == "palmimo" and (
        not official or source["url"].lower() != "https://github.com/jizai-inc/palmimo-apps"
    ):
        raise ValueError("untrusted official source")
    if official and namespace != "palmimo":
        logger.warning("public catalog item %s: downgraded official", index)
        official = False
    raw_translations = item.get("translations")
    translations = {
        locale: {key: value.strip() for key, value in messages.items()}
        for locale, messages in parse_translations(raw_translations).items()
    }
    if "translations" in item and raw_translations != translations:
        logger.warning("public catalog item %s: dropped or normalized translation fields", index)
    return PublicCatalogApp(
        name=item["name"],
        description=item["description"],
        source=AppSource(
            type="git",
            url=source["url"],
            ref=source["ref"],
            ref_kind=source["ref_kind"],
            subdir=subdir,
            manifest=manifest,
            commit=source["commit"],
        ),
        public=PublicAppIdentity(public["app_id"], namespace, official),
        env=env,
        devices=tuple(devices),
        translations=translations,
    )


def parse_public_catalog(data: bytes) -> PublicCatalogAsset:
    try:
        if len(data) > MAX_CATALOG_BYTES:
            raise ValueError("catalog exceeds byte limit")
        payload = json.loads(data)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise PublicCatalogSourceError(str(error)) from error
    return parse_public_catalog_payload(payload)


def parse_public_catalog_payload(payload: Any) -> PublicCatalogAsset:
    try:
        envelope = _object(payload)
        if type(envelope.get("schema")) is not int or envelope["schema"] != 1:
            raise ValueError("invalid catalog schema")
        generated_at = envelope.get("generated_at")
        if not isinstance(generated_at, str) or not _matches(generated_at, _TIMESTAMP):
            raise ValueError("invalid generated_at")
        datetime.fromisoformat(generated_at)
        raw_apps = envelope.get("apps")
        if not isinstance(raw_apps, list) or len(raw_apps) > MAX_CATALOG_APPS:
            raise ValueError("invalid apps or catalog exceeds app limit")
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise PublicCatalogSourceError(str(error)) from error
    parsed: list[tuple[int, PublicCatalogApp]] = []
    for index, raw in enumerate(raw_apps):
        try:
            parsed.append((index, _parse_item(raw, index)))
        except (ValueError, KeyError, TypeError) as error:
            logger.warning("public catalog item %s: discarded: %s", index, error)
    ids = Counter(app.public.app_id for _, app in parsed)
    names = Counter((app.public.namespace, app.name) for _, app in parsed)
    apps = []
    for index, app in parsed:
        if ids[app.public.app_id] > 1 or names[(app.public.namespace, app.name)] > 1:
            logger.warning("public catalog item %s: discarded duplicate", index)
        else:
            apps.append(app)
    return PublicCatalogAsset(generated_at, tuple(apps))


@dataclass
class HttpPublicCatalogSource(PublicCatalogSource):
    public_catalog_url: str = DEFAULT_PUBLIC_CATALOG_URL
    opener: Opener = field(default=default_opener)

    def fetch(self) -> PublicCatalogAsset:
        try:
            data = download(
                self.opener,
                self.public_catalog_url,
                f"palmimo-portal/{portal_version()}",
                max_bytes=MAX_CATALOG_BYTES,
            )
            return parse_public_catalog(data)
        except StaticAssetError as error:
            raise PublicCatalogSourceError(str(error)) from error
