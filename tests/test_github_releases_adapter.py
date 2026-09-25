"""Tests for :mod:`palmimo_portal.adapters.github_releases`. No network access -- an injected opener stands in."""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from palmimo_portal.adapters.github_releases import GitHubReleaseSource
from palmimo_portal.ports import Release, ReleaseSourceError


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _opener_returning(payload: Any) -> Any:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        return _FakeResponse(payload)

    return opener


def _opener_raising(error: Exception) -> Any:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise error

    return opener


VALID_PAYLOAD = {
    "tag_name": "v2.0.0",
    "name": "v2.0.0",
    "published_at": "2026-01-01T00:00:00Z",
    "html_url": "https://github.com/Jizai-inc/palmimo-portal/releases/tag/v2.0.0",
}


def test_fetch_latest_parses_the_release() -> None:
    source = GitHubReleaseSource(repo="Jizai-inc/palmimo-portal", opener=_opener_returning(VALID_PAYLOAD))

    release = source.fetch_latest()

    assert release == Release(
        tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url=VALID_PAYLOAD["html_url"]
    )


def test_fetch_latest_sends_the_expected_headers() -> None:
    captured: dict[str, urllib.request.Request] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["request"] = request
        return _FakeResponse(VALID_PAYLOAD)

    source = GitHubReleaseSource(repo="Jizai-inc/palmimo-portal", opener=opener)

    source.fetch_latest()

    request = captured["request"]
    assert request.full_url == "https://api.github.com/repos/Jizai-inc/palmimo-portal/releases/latest"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("User-agent", "").startswith("palmimo-portal/")


def test_fetch_latest_raises_no_release_on_a_404() -> None:
    error = urllib.error.HTTPError("url", 404, "Not Found", email.message.Message(), io.BytesIO(b""))
    source = GitHubReleaseSource(opener=_opener_raising(error))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "no_release"


@pytest.mark.parametrize("status", [500, 503])
def test_fetch_latest_raises_release_source_unavailable_on_other_http_errors(status: int) -> None:
    error = urllib.error.HTTPError("url", status, "Server Error", email.message.Message(), io.BytesIO(b""))
    source = GitHubReleaseSource(opener=_opener_raising(error))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


def test_fetch_latest_raises_rate_limited_on_429() -> None:
    error = urllib.error.HTTPError("url", 429, "Too Many Requests", email.message.Message(), io.BytesIO(b""))
    source = GitHubReleaseSource(opener=_opener_raising(error))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "rate_limited"


def test_fetch_latest_raises_rate_limited_on_403_with_remaining_quota_exhausted() -> None:
    headers = email.message.Message()
    headers["X-RateLimit-Remaining"] = "0"
    error = urllib.error.HTTPError("url", 403, "Forbidden", headers, io.BytesIO(b""))
    source = GitHubReleaseSource(opener=_opener_raising(error))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "rate_limited"


def test_fetch_latest_raises_release_source_unavailable_on_403_without_the_rate_limit_header() -> None:
    # A plain 403 (no quota header, or a nonzero remaining count) is a permissions problem,
    # not rate limiting -- it must not be reported as retryable-after-a-wait.
    error = urllib.error.HTTPError("url", 403, "Forbidden", email.message.Message(), io.BytesIO(b""))
    source = GitHubReleaseSource(opener=_opener_raising(error))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


def test_fetch_latest_raises_release_source_unavailable_on_a_url_error() -> None:
    source = GitHubReleaseSource(opener=_opener_raising(urllib.error.URLError("network unreachable")))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


def test_fetch_latest_raises_release_source_unavailable_on_a_timeout() -> None:
    source = GitHubReleaseSource(opener=_opener_raising(TimeoutError("timed out")))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


def test_fetch_latest_raises_release_source_unavailable_on_malformed_json() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> Any:
        class _BadResponse:
            def read(self) -> bytes:
                return b"not json {{{"

            def __enter__(self) -> _BadResponse:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        return _BadResponse()

    source = GitHubReleaseSource(opener=opener)

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


def test_fetch_latest_raises_release_source_unavailable_on_an_unexpected_shape() -> None:
    source = GitHubReleaseSource(opener=_opener_returning({"unexpected": "shape"}))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "release_source_unavailable"


PRERELEASE_PAYLOAD = {
    "tag_name": "v2.0.0-rc1",
    "name": "v2.0.0-rc1",
    "published_at": "2026-02-01T00:00:00Z",
    "html_url": "https://github.com/Jizai-inc/palmimo-portal/releases/tag/v2.0.0-rc1",
    "draft": False,
    "prerelease": True,
}


@pytest.mark.parametrize(
    ("channel", "expected_tag"),
    [
        ("stable", "examples-v0.1.0"),
        ("prerelease", "examples-v0.2.0-rc1"),
    ],
)
def test_fetch_latest_with_a_tag_prefix_selects_the_newest_matching_release(channel: str, expected_tag: str) -> None:
    payload = [
        {**VALID_PAYLOAD, "tag_name": "v0.1.1", "draft": False, "prerelease": False},
        {**PRERELEASE_PAYLOAD, "tag_name": "examples-v0.2.0-rc1", "prerelease": True},
        {**VALID_PAYLOAD, "tag_name": "examples-v0.1.0", "draft": False, "prerelease": False},
    ]
    source = GitHubReleaseSource(channel=channel, tag_prefix="examples-v", opener=_opener_returning(payload))

    release = source.fetch_latest()

    assert release.tag == expected_tag


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{**VALID_PAYLOAD, "tag_name": "v0.1.1", "draft": False, "prerelease": False}],
        [{**VALID_PAYLOAD, "tag_name": "examples-v0.1.0", "draft": True, "prerelease": False}],
    ],
    ids=["empty", "no_matching_tag", "matching_draft"],
)
def test_fetch_latest_with_a_tag_prefix_raises_no_release_when_nothing_matches(payload: list[dict[str, Any]]) -> None:
    source = GitHubReleaseSource(channel="stable", tag_prefix="examples-v", opener=_opener_returning(payload))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "no_release"


def test_fetch_latest_with_a_tag_prefix_looks_past_a_full_page_of_other_releases() -> None:
    # devkit keeps cutting SDK releases after an examples release; once more
    # than a page of them pile up, the catalog release is on page two.
    import urllib.parse

    sdk_page = [
        {**VALID_PAYLOAD, "tag_name": f"v0.1.{n}", "draft": False, "prerelease": False} for n in range(100, 0, -1)
    ]
    pages = {1: sdk_page, 2: [{**VALID_PAYLOAD, "tag_name": "examples-v0.1.0", "draft": False, "prerelease": False}]}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        return _FakeResponse(pages.get(int(query.get("page", ["1"])[0]), []))

    source = GitHubReleaseSource(channel="stable", tag_prefix="examples-v", opener=opener)

    assert source.fetch_latest().tag == "examples-v0.1.0"


def test_fetch_latest_on_the_prerelease_channel_selects_the_highest_non_draft_version() -> None:
    payload = [
        {**PRERELEASE_PAYLOAD, "draft": True, "tag_name": "v2.0.0-rc2-draft"},
        PRERELEASE_PAYLOAD,
        {**VALID_PAYLOAD, "draft": False},
    ]
    source = GitHubReleaseSource(
        repo="Jizai-inc/palmimo-portal", channel="prerelease", opener=_opener_returning(payload)
    )

    release = source.fetch_latest()

    assert release.tag == "v2.0.0"


PORTAL_RELEASES_IN_API_ORDER = [
    {**PRERELEASE_PAYLOAD, "tag_name": "v0.2.0-rc9"},
    {**PRERELEASE_PAYLOAD, "tag_name": "v0.2.0-rc10"},
    {**PRERELEASE_PAYLOAD, "tag_name": "v0.2.0-rc8"},
    {**PRERELEASE_PAYLOAD, "tag_name": "v0.2.0-rc7"},
    {**VALID_PAYLOAD, "tag_name": "v0.1.6", "draft": False, "prerelease": False},
    {**VALID_PAYLOAD, "tag_name": "v0.1.5", "draft": False, "prerelease": False},
]

DEVKIT_RELEASES_IN_API_ORDER = [
    {**PRERELEASE_PAYLOAD, "tag_name": "examples-v0.1.0-rc3"},
    {**VALID_PAYLOAD, "tag_name": "v0.1.1", "draft": False, "prerelease": False},
    {**PRERELEASE_PAYLOAD, "tag_name": "examples-v0.1.0-rc2"},
    {**PRERELEASE_PAYLOAD, "tag_name": "examples-v0.1.0-rc1"},
    {**VALID_PAYLOAD, "tag_name": "v0.1.0", "draft": False, "prerelease": False},
]


@pytest.mark.parametrize(
    ("channel", "tag_prefix", "payload", "expected_tag", "expected_error"),
    [
        ("prerelease", None, PORTAL_RELEASES_IN_API_ORDER, "v0.2.0-rc10", None),
        ("prerelease", "examples-v", DEVKIT_RELEASES_IN_API_ORDER, "examples-v0.1.0-rc3", None),
        ("stable", "examples-v", DEVKIT_RELEASES_IN_API_ORDER, None, "no_release"),
        (
            "stable",
            "examples-v",
            [*DEVKIT_RELEASES_IN_API_ORDER, {**VALID_PAYLOAD, "tag_name": "examples-v0.1.0", "draft": False}],
            "examples-v0.1.0",
            None,
        ),
        (
            "prerelease",
            "examples-v",
            [*DEVKIT_RELEASES_IN_API_ORDER, {**VALID_PAYLOAD, "tag_name": "examples-v0.1.0", "draft": False}],
            "examples-v0.1.0",
            None,
        ),
        (
            "stable",
            "examples-v",
            [
                *DEVKIT_RELEASES_IN_API_ORDER,
                {**VALID_PAYLOAD, "tag_name": "examples-v0.1.1", "draft": False},
                {**PRERELEASE_PAYLOAD, "tag_name": "examples-v0.2.0-rc1"},
            ],
            "examples-v0.1.1",
            None,
        ),
    ],
    ids=[
        "portal_prerelease",
        "catalog_prerelease",
        "catalog_stable_without_final",
        "catalog_stable_with_final",
        "catalog_prerelease_prefers_final",
        "catalog_stable_ignores_newer_prerelease",
    ],
)
def test_fetch_latest_from_list_selects_the_highest_matching_version(
    channel: str,
    tag_prefix: str | None,
    payload: list[dict[str, Any]],
    expected_tag: str | None,
    expected_error: str | None,
) -> None:
    source = GitHubReleaseSource(channel=channel, tag_prefix=tag_prefix, opener=_opener_returning(payload))

    if expected_error is not None:
        with pytest.raises(ReleaseSourceError) as excinfo:
            source.fetch_latest()

        assert excinfo.value.code == expected_error
    else:
        assert source.fetch_latest().tag == expected_tag


def test_fetch_latest_on_the_prerelease_channel_uses_the_release_list_url() -> None:
    captured: dict[str, urllib.request.Request] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["request"] = request
        return _FakeResponse([PRERELEASE_PAYLOAD])

    source = GitHubReleaseSource(repo="Jizai-inc/palmimo-portal", channel="prerelease", opener=opener)

    source.fetch_latest()

    request = captured["request"]
    assert request.full_url == "https://api.github.com/repos/Jizai-inc/palmimo-portal/releases?per_page=100&page=1"


def test_fetch_latest_on_the_prerelease_channel_raises_no_release_when_only_drafts_exist() -> None:
    payload = [{**PRERELEASE_PAYLOAD, "draft": True}]
    source = GitHubReleaseSource(channel="prerelease", opener=_opener_returning(payload))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "no_release"


def test_fetch_latest_on_the_prerelease_channel_raises_no_release_on_an_empty_list() -> None:
    source = GitHubReleaseSource(channel="prerelease", opener=_opener_returning([]))

    with pytest.raises(ReleaseSourceError) as excinfo:
        source.fetch_latest()

    assert excinfo.value.code == "no_release"


def test_fetch_latest_on_the_stable_channel_still_uses_the_latest_endpoint() -> None:
    captured: dict[str, urllib.request.Request] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["request"] = request
        return _FakeResponse(VALID_PAYLOAD)

    source = GitHubReleaseSource(repo="Jizai-inc/palmimo-portal", channel="stable", opener=opener)

    source.fetch_latest()

    request = captured["request"]
    assert request.full_url == "https://api.github.com/repos/Jizai-inc/palmimo-portal/releases/latest"
