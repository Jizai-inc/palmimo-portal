from dataclasses import dataclass

from palmimo_portal.ports import AppRecord


@dataclass(frozen=True)
class LegacyOfficial:
    name: str | None


_OFFICIAL_APPS = {
    ("examples/agents/companion", "palmimo.realtime.toml"): LegacyOfficial("companion-realtime"),
    ("examples/teleop", "palmimo.toml"): LegacyOfficial("teleop"),
    ("examples/agents/companion", "palmimo.toml"): LegacyOfficial(None),
    ("examples/agents/wakeword", "palmimo.toml"): LegacyOfficial(None),
}


def legacy_official(record: AppRecord) -> LegacyOfficial | None:
    source = record.source
    if (
        record.id.partition(".")[0] != "palmimo"
        or source.public_app_id is not None
        or source.type != "git"
        or source.ref_kind != "tag"
        or not (source.ref or "").startswith("examples-v")
        or (source.url or "").lower().rstrip("/").removesuffix(".git") != "https://github.com/jizai-inc/palmimo-devkit"
    ):
        return None
    return _OFFICIAL_APPS.get((source.subdir or "", source.manifest or "palmimo.toml"))


def is_retired_official(record: AppRecord) -> bool:
    legacy = legacy_official(record)
    return legacy is not None and legacy.name is None
