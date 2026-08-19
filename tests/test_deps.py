"""Unit tests for :mod:`palmimo_portal.api.deps` translation-layer behavior.

Most of ``deps.py`` is already exercised end-to-end through the ``api/``
integration tests (``test_api_provisioning_gate.py`` and friends). This
module covers the parts that matter at the unit level: that
:func:`~palmimo_portal.api.deps.require_provisioned` *delegates* to the one
canonical rule in :mod:`palmimo_portal.core.provisioning` rather than
re-implementing it -- see the technical-review item this closes ("Unify
require_provisioned").
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import Request

from palmimo_portal.api.deps import require_provisioned
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.provisioning import NotProvisionedError
from palmimo_portal.testing.fakes import FakeNetworkPort


def test_require_provisioned_delegates_to_the_core_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    import palmimo_portal.api.deps as deps_module

    network = FakeNetworkPort()
    calls: list[Any] = []

    def fake_core_require_provisioned(passed_network: Any) -> None:
        calls.append(passed_network)

    monkeypatch.setattr(deps_module, "_core_require_provisioned", fake_core_require_provisioned)

    # `request` is unused by the delegating implementation (see the function's
    # own docstring); a real Request is not needed to exercise the delegation.
    require_provisioned(request=cast(Request, None), network=network)

    # The exact same NetworkPort instance reaches the core rule -- proof
    # this is a delegating call, not a second, independent reimplementation
    # of "is_provisioned(network)" living in this module too.
    assert calls == [network]


def test_require_provisioned_translates_not_provisioned_to_a_409_portal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import palmimo_portal.api.deps as deps_module

    def fake_core_require_provisioned(_network: Any) -> None:
        raise NotProvisionedError()

    monkeypatch.setattr(deps_module, "_core_require_provisioned", fake_core_require_provisioned)

    with pytest.raises(PortalError) as excinfo:
        require_provisioned(request=cast(Request, None), network=FakeNetworkPort())

    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "not_provisioned"
