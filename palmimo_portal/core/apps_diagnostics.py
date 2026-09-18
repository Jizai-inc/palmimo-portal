"""Builds the plain-text support document for ``GET /apps/{name}/diagnostics`` (design doc 3.2).

One flat document, fixed section headers, no secret values or git tokens --
:func:`build_diagnostics` never reads a secret store itself; the caller
passes every known secret/token value in ``known_secret_values`` and this
module scrubs them from the *whole* assembled text (including journal
lines, which can carry app-printed output this module never generated) via
:func:`~palmimo_portal.core.secrets.mask_known_values`, not just the
fields it wrote itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from palmimo_portal.core.apps_start import PrecheckResult
from palmimo_portal.core.manifest import Manifest
from palmimo_portal.core.secrets import mask_known_values
from palmimo_portal.ports import AppRecord, UnitStatus


SECTION_PORTAL = "[portal]"
SECTION_PLATFORM = "[platform]"
SECTION_UV = "[uv]"
SECTION_APP = "[app]"
SECTION_MANIFEST = "[manifest]"
SECTION_PRECHECK = "[precheck]"
SECTION_UNIT = "[unit]"
SECTION_JOURNAL = "[journal]"
SECTION_SYSTEM = "[system]"

#: Section headers, in the fixed order the document is assembled in.
SECTIONS: tuple[str, ...] = (
    SECTION_PORTAL,
    SECTION_PLATFORM,
    SECTION_UV,
    SECTION_APP,
    SECTION_MANIFEST,
    SECTION_PRECHECK,
    SECTION_UNIT,
    SECTION_JOURNAL,
    SECTION_SYSTEM,
)

#: Answered when this process cannot read the journal at all (design doc 3.6), mirroring
#: LogsResponse's own `"journal_permission"` sentinel (api/apps.py).
JOURNAL_UNAVAILABLE_LINE = "unavailable: journal_permission"


@dataclass(frozen=True)
class AppDiagnosticsInput:
    """Every already-gathered piece of data :func:`build_diagnostics` assembles into text."""

    portal_version: str
    platform_status: dict[str, Any]
    uv_version: str
    record: AppRecord
    manifest: Manifest | None
    manifest_errors: list[str]
    bindings: dict[str, str]
    precheck: PrecheckResult
    unit_status: UnitStatus
    #: `None` means the journal could not be read at all; an empty list means it was
    #: read and is simply empty -- distinct outcomes (mirrors LogsResponse.unavailable).
    journal_lines: list[str] | None
    disk_free_bytes: int
    ntp_synchronized: bool
    hostname: str
    device_id: str | None
    portal_update_job_state: str


def _scrub_userinfo(url: str | None) -> str | None:
    """Strip a ``user:pass@``/``user@`` prefix from ``url``'s netloc, if any.

    Defense in depth: ``validate_git_url`` already refuses userinfo at the
    API boundary, but this document must never echo a credential back even
    for a ledger record written before that check existed.
    """
    if url is None:
        return None
    parsed = urlparse(url)
    if not parsed.username and not parsed.password:
        return url
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _precheck_line(precheck: PrecheckResult) -> str:
    if precheck.ok:
        return "ok"
    parts = [f"code={precheck.code}"]
    if precheck.names:
        parts.append(f"names={precheck.names}")
    if precheck.running:
        parts.append(f"running={precheck.running}")
    return " ".join(parts)


def build_diagnostics(data: AppDiagnosticsInput, *, known_secret_values: list[str]) -> str:
    """Render one flat support document, with every registered secret value/token masked out."""
    record = data.record
    source = record.source
    last_job = record.last_job
    lines: list[str] = [
        SECTION_PORTAL,
        f"version={data.portal_version}",
        f"update_job_state={data.portal_update_job_state}",
        "",
        SECTION_PLATFORM,
        f"installed_version={data.platform_status.get('installed_version')}",
        f"required_version={data.platform_status.get('required_version')}",
        f"ready={data.platform_status.get('ready')}",
        f"verify_diffs={data.platform_status.get('verify_diffs')}",
        "",
        SECTION_UV,
        f"version={data.uv_version}",
        "",
        SECTION_APP,
        f"name={record.name}",
        f"source.type={source.type}",
        f"source.url={_scrub_userinfo(source.url)}",
        f"source.subdir={source.subdir}",
        f"source.ref_kind={source.ref_kind}",
        f"source.ref={source.ref}",
        f"source.commit={source.commit}",
        f"installed_at={record.installed_at}",
        f"autostart={record.autostart}",
        f"params={dict(sorted(record.params.items()))}",
        f"last_job.kind={last_job.kind if last_job else None}",
        f"last_job.state={last_job.state if last_job else None}",
        f"last_job.step={last_job.step if last_job else None}",
        f"last_job.error={last_job.error if last_job else None}",
        *(f"binding {request_name} <- {store_name}" for request_name, store_name in sorted(data.bindings.items())),
        "",
        SECTION_MANIFEST,
        *(["ok"] if data.manifest is not None else [f"error: {error}" for error in data.manifest_errors]),
        "",
        SECTION_PRECHECK,
        _precheck_line(data.precheck),
        "",
        SECTION_UNIT,
        f"ActiveState={data.unit_status.active_state}",
        f"SubState={data.unit_status.sub_state}",
        f"Result={data.unit_status.result}",
        f"ExecMainStatus={data.unit_status.exec_main_status}",
        f"ExecMainStartTimestamp={data.unit_status.exec_main_start_timestamp}",
        "",
        SECTION_JOURNAL,
        *(data.journal_lines if data.journal_lines is not None else [JOURNAL_UNAVAILABLE_LINE]),
        "",
        SECTION_SYSTEM,
        f"hostname={data.hostname}",
        f"device_id={data.device_id}",
        f"disk_free_bytes={data.disk_free_bytes}",
        f"ntp_synchronized={data.ntp_synchronized}",
    ]
    text = "\n".join(lines) + "\n"
    return mask_known_values(text, known_secret_values)
