"""Behavioral tests for namespace-qualified app identity."""

from __future__ import annotations

import pytest

from palmimo_portal.core.apps import app_namespace, normalize_namespace, suggest_app_name
from palmimo_portal.ports import AppsState


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Jizai-inc/palmimo-devkit",
        "https://github.com/Jizai-inc/palmimo-devkit.git",
        "https://GITHUB.COM/JIZAI-INC/palmimo-devkit/",
    ],
)
def test_app_namespace_recognizes_normalized_official_sources(url: str) -> None:
    assert app_namespace("git", url, "Jizai-inc/palmimo-devkit") == "palmimo"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo", "acme"),
        ("https://github.com/zip/repo", "zip-gh"),
        ("https://git.example.test:8443/acme/repo", "git-example-test"),
        ("https://github.example.test/acme/repo", "github-example-test"),
    ],
)
def test_app_namespace_derives_source_namespace(url: str, expected: str) -> None:
    assert app_namespace("git", url, "Jizai-inc/palmimo-devkit") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a" * 39 + "--", "a" * 39), ("---", "git")],
)
def test_normalize_namespace_keeps_valid_boundary(value: str, expected: str) -> None:
    assert normalize_namespace(value) == expected


def test_suggest_app_name_uses_next_suffix_when_name_is_occupied() -> None:
    state = AppsState(current_job_app="palmimo.teleop")
    assert suggest_app_name("palmimo", "teleop", state) == "teleop-2"


def test_suggest_app_name_keeps_suffix_within_name_limit() -> None:
    state = AppsState(apps={f"palmimo.{'a' * 40}": object()})  # type: ignore[dict-item]
    assert suggest_app_name("palmimo", "a" * 40, state) == f"{'a' * 38}-2"


def test_suggest_app_name_skips_past_an_already_occupied_suffix() -> None:
    # Without scanning past -2, a hand-picked "teleop-2" collision would loop forever
    # re-offering the same taken suffix instead of finding the next free one.
    state = AppsState(apps={"palmimo.teleop": object(), "palmimo.teleop-2": object()})  # type: ignore[dict-item]
    assert suggest_app_name("palmimo", "teleop", state) == "teleop-3"


def test_suggest_app_name_lets_the_same_name_coexist_under_different_namespaces() -> None:
    # Suggestion must be scoped to its own namespace: an id already taken by
    # a different owner's app must not push this owner's first pick to -2.
    state = AppsState(apps={"alice.teleop": object()})  # type: ignore[dict-item]
    assert suggest_app_name("bob", "teleop", state) == "teleop"
