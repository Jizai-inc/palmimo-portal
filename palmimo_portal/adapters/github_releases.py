"""Real :class:`~palmimo_portal.ports.ReleaseSource`: GitHub's Releases API.

``channel == "stable"`` without a tag prefix calls ``GET
/repos/<repo>/releases/latest`` -- GitHub's own endpoint for "the most
recent non-prerelease, non-draft release". With a tag prefix, or with
``channel == "prerelease"`` (the dev-machine opt-in -- see
``PALMIMO_UPDATE_CHANNEL`` in ``settings.py``), it instead walks the release
list newest first, a page at a time, and picks the first non-draft release
the channel admits (prereleases only on the prerelease channel) whose tag
has the prefix. That keeps a published rc discoverable without disturbing
``releases/latest`` for every other device, and lets the catalog pick its
own releases out of a repository that also publishes others. Uses
``urllib`` (stdlib), not ``httpx``/``requests``: one occasional request
doesn't need an HTTP client.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from palmimo_portal.ports import Release, ReleaseSource, ReleaseSourceError
from palmimo_portal.version import portal_version


logger = logging.getLogger("palmimo_portal")

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Page size for the list-based selection: the API maximum. GitHub orders
#: this endpoint by created date descending.
PRERELEASE_LIST_PAGE_SIZE = 100

#: Pages the list-based selection walks before giving up. A tag prefix has to
#: see past runs of other releases (devkit interleaves SDK releases and rcs
#: with its examples releases); each page costs one unauthenticated API call.
RELEASE_LIST_MAX_PAGES = 10

#: What :attr:`GitHubReleaseSource.opener` is called with: a fully-built
#: :class:`urllib.request.Request` and the timeout in seconds. Must return a
#: context manager yielding an object with a ``.read()`` method, exactly what
#: :func:`urllib.request.urlopen` returns.
Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def _is_rate_limited(error: urllib.error.HTTPError) -> bool:
    """GitHub signals API rate limiting as 429, or 403 with ``X-RateLimit-Remaining: 0``.

    A plain 403 (no such header, or a nonzero remaining count) is a
    permissions/auth problem, not rate limiting -- reported as the generic
    ``release_source_unavailable`` instead.
    """
    if error.code == 429:
        return True
    return error.code == 403 and error.headers is not None and error.headers.get("X-RateLimit-Remaining") == "0"


def _release_from_payload(payload: Any) -> Release:
    try:
        return Release(
            tag=payload["tag_name"],
            name=payload["name"],
            published_at=payload["published_at"],
            html_url=payload["html_url"],
        )
    except (KeyError, TypeError) as error:
        raise ReleaseSourceError("release_source_unavailable", f"unexpected response shape: {error}") from error


@dataclass
class GitHubReleaseSource(ReleaseSource):
    """Fetches ``repo``'s latest release from the GitHub API, per :attr:`channel`.

    ``opener`` is the test seam: unit tests inject a fake returning a
    canned response instead of making a real HTTP request -- see
    ``tests/test_github_releases_adapter.py``.
    """

    repo: str = "Jizai-inc/palmimo-portal"
    channel: str = "stable"
    tag_prefix: str | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    opener: Opener = field(default=_default_opener)

    def fetch_latest(self) -> Release:
        if self.channel == "prerelease" or self.tag_prefix is not None:
            return self._fetch_latest_from_list()
        return self._fetch_latest_from_latest_endpoint()

    def _request(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"palmimo-portal/{portal_version()}",
            },
        )
        try:
            with self.opener(request, self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ReleaseSourceError("no_release", f"no releases found for {self.repo}") from error
            if _is_rate_limited(error):
                reset = error.headers.get("X-RateLimit-Reset") if error.headers is not None else None
                if reset is not None:
                    logger.warning("github: rate limited for %s, resets at epoch=%s", self.repo, reset)
                raise ReleaseSourceError("rate_limited", f"GitHub API rate limit exceeded for {self.repo}") from error
            raise ReleaseSourceError(
                "release_source_unavailable", f"GitHub API returned HTTP {error.code} for {self.repo}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ReleaseSourceError("release_source_unavailable", str(error)) from error

    def _fetch_latest_from_latest_endpoint(self) -> Release:
        payload = self._request(f"https://api.github.com/repos/{self.repo}/releases/latest")
        return _release_from_payload(payload)

    def _fetch_latest_from_list(self) -> Release:
        for page in range(1, RELEASE_LIST_MAX_PAGES + 1):
            payload = self._request(
                f"https://api.github.com/repos/{self.repo}/releases?per_page={PRERELEASE_LIST_PAGE_SIZE}&page={page}"
            )
            if not isinstance(payload, list):
                raise ReleaseSourceError("release_source_unavailable", f"unexpected response shape: {payload!r}")
            for entry in payload:
                if not isinstance(entry, dict) or entry.get("draft", False):
                    continue
                if self.channel != "prerelease" and entry.get("prerelease", False):
                    continue
                if self.tag_prefix is not None and not str(entry.get("tag_name", "")).startswith(self.tag_prefix):
                    continue
                return _release_from_payload(entry)
            if len(payload) < PRERELEASE_LIST_PAGE_SIZE:
                break
        raise ReleaseSourceError("no_release", f"no releases found for {self.repo}")
