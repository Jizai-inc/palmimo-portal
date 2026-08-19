"""Real :class:`~palmimo_portal.ports.ReleaseSource`: GitHub's ``releases/latest`` API.

``GET https://api.github.com/repos/<repo>/releases/latest`` -- GitHub's own
endpoint for "the most recent non-prerelease, non-draft release", so this
adapter never filters a release list itself. Uses ``urllib`` (stdlib), not
``httpx``/``requests``: one occasional request doesn't need an HTTP client.
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

#: What :attr:`GitHubReleaseSource.opener` is called with: a fully-built
#: :class:`urllib.request.Request` and the timeout in seconds. Must return a
#: context manager yielding an object with a ``.read()`` method, exactly what
#: :func:`urllib.request.urlopen` returns.
Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


@dataclass
class GitHubReleaseSource(ReleaseSource):
    """Fetches ``repo``'s latest release from the GitHub API.

    ``opener`` is the test seam: unit tests inject a fake returning a
    canned response instead of making a real HTTP request -- see
    ``tests/test_github_releases_adapter.py``.
    """

    repo: str = "Jizai-inc/palmimo-portal"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    opener: Opener = field(default=_default_opener)

    def fetch_latest(self) -> Release:
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"palmimo-portal/{portal_version()}",
            },
        )
        try:
            with self.opener(request, self.timeout) as response:
                payload: Any = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ReleaseSourceError("no_release", f"no releases found for {self.repo}") from error
            raise ReleaseSourceError(
                "release_source_unavailable", f"GitHub API returned HTTP {error.code} for {self.repo}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise ReleaseSourceError("release_source_unavailable", str(error)) from error

        try:
            return Release(
                tag=payload["tag_name"],
                name=payload["name"],
                published_at=payload["published_at"],
                html_url=payload["html_url"],
            )
        except (KeyError, TypeError) as error:
            raise ReleaseSourceError("release_source_unavailable", f"unexpected response shape: {error}") from error
