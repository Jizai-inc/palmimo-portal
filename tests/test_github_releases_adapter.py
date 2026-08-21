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
}


def test_fetch_latest_on_the_prerelease_channel_resolves_the_newest_non_draft_entry() -> None:
    payload = [
        {**PRERELEASE_PAYLOAD, "draft": True, "tag_name": "v2.0.0-rc2-draft"},
        PRERELEASE_PAYLOAD,
        {**VALID_PAYLOAD, "draft": False},
    ]
    source = GitHubReleaseSource(
        repo="Jizai-inc/palmimo-portal", channel="prerelease", opener=_opener_returning(payload)
    )

    release = source.fetch_latest()

    assert release.tag == "v2.0.0-rc1"


def test_fetch_latest_on_the_prerelease_channel_uses_the_release_list_url() -> None:
    captured: dict[str, urllib.request.Request] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["request"] = request
        return _FakeResponse([PRERELEASE_PAYLOAD])

    source = GitHubReleaseSource(repo="Jizai-inc/palmimo-portal", channel="prerelease", opener=opener)

    source.fetch_latest()

    request = captured["request"]
    assert request.full_url == "https://api.github.com/repos/Jizai-inc/palmimo-portal/releases?per_page=10"


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
