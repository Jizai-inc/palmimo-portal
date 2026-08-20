"""CI-safe tests for :mod:`palmimo_portal.adapters.systemd`.

No real D-Bus connection: :meth:`SystemdSystemPort._call` is stubbed by
subclassing, the same pattern as ``test_comitup_adapter.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast

import pytest
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface

from palmimo_portal.adapters.systemd import SystemdSystemPort
from palmimo_portal.ports import AdapterUnavailableError


class _StubbedSystemPort(SystemdSystemPort):
    def __init__(self, script: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script: dict[str, Any] = script or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.calls.append((member, args))
        outcome = self.script[member]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _call_systemd(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.calls.append((member, args))
        outcome = self.script[member]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_reboot_calls_logind_reboot_non_interactively() -> None:
    port = _StubbedSystemPort({"reboot": None})

    port.reboot()

    assert port.calls == [("reboot", (False,))]


def test_shutdown_calls_logind_power_off_non_interactively() -> None:
    port = _StubbedSystemPort({"power_off": None})

    port.shutdown()

    assert port.calls == [("power_off", (False,))]


def test_reboot_raises_adapter_unavailable_when_the_dbus_call_fails_twice() -> None:
    port = _StubbedSystemPort({"reboot": TimeoutError("no reply")})

    with pytest.raises(AdapterUnavailableError):
        port.reboot()


def test_shutdown_raises_adapter_unavailable_when_the_dbus_call_fails_twice() -> None:
    port = _StubbedSystemPort({"power_off": ConnectionError("bus gone")})

    with pytest.raises(AdapterUnavailableError):
        port.shutdown()


def test_restart_portal_calls_restart_unit_with_the_configured_unit_and_replace_mode() -> None:
    port = _StubbedSystemPort({"restart_unit": None}, unit="palmimo-portal.service")

    port.restart_portal()

    assert port.calls == [("restart_unit", ("palmimo-portal.service", "replace"))]


def test_restart_portal_uses_a_custom_unit_name() -> None:
    port = _StubbedSystemPort({"restart_unit": None}, unit="custom-portal.service")

    port.restart_portal()

    assert port.calls == [("restart_unit", ("custom-portal.service", "replace"))]


def test_restart_portal_raises_adapter_unavailable_when_the_dbus_call_fails_twice() -> None:
    port = _StubbedSystemPort({"restart_unit": ConnectionError("bus gone")})

    with pytest.raises(AdapterUnavailableError):
        port.restart_portal()


class _FlakyOnceSystemPort(SystemdSystemPort):
    """Fails the first ``_call`` for a member, then succeeds -- exercises the retry path.

    Overrides ``_call`` directly, bypassing the real ``_connect``/
    ``_disconnect``/``_lock`` machinery -- see
    ``test_comitup_adapter.py``'s equivalent class docstring. The
    identity-guarded disconnect and timeout-never-retries behavior are
    proven below against the real ``_connect``/``_disconnect`` pair.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attempts: dict[str, int] = {}

    async def _call(self, member: str, args: tuple[Any, ...], timeout: float) -> Any:
        self.attempts[member] = self.attempts.get(member, 0) + 1
        if self.attempts[member] == 1:
            raise ConnectionError("logind connection dropped")
        return None


def test_reboot_reconnects_and_retries_once_after_a_dropped_connection() -> None:
    port = _FlakyOnceSystemPort()

    port.reboot()

    assert port.attempts["reboot"] == 2


class _FakeBus:
    def __init__(self, label: str) -> None:
        self.label = label
        self.disconnect_calls = 0

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class _FakeInterface:
    def __init__(self, *, healthy: bool) -> None:
        self.healthy = healthy
        self.call_count = 0

    async def call_reboot(self, interactive: bool) -> Any:
        self.call_count += 1
        if not self.healthy:
            raise ConnectionError("bus dropped")
        return None


class _OpenBusScriptedSystemPort(SystemdSystemPort):
    def __init__(self, open_bus_script: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._open_bus_script = list(open_bus_script)
        self.open_bus_calls = 0

    async def _open_bus(self) -> tuple[Any, Any]:
        self.open_bus_calls += 1
        outcome = self._open_bus_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_concurrent_calls_over_a_dropped_bus_reconnect_exactly_once() -> None:
    old_bus = _FakeBus("old")
    new_bus = _FakeBus("new")
    new_interface = _FakeInterface(healthy=True)
    port = _OpenBusScriptedSystemPort([(new_bus, new_interface)])
    port._bus = cast(MessageBus, old_bus)
    port._interface = cast(ProxyInterface, _FakeInterface(healthy=False))

    first, second = await asyncio.gather(
        port._call_resilient("reboot", (False,), 5.0),
        port._call_resilient("reboot", (False,), 5.0),
    )

    assert first is None
    assert second is None
    assert port.open_bus_calls == 1
    assert old_bus.disconnect_calls == 1
    assert new_bus.disconnect_calls == 0


async def test_a_stale_disconnect_does_not_tear_down_a_bus_another_call_already_replaced() -> None:
    old_bus = _FakeBus("old")
    new_bus = _FakeBus("new")
    port = _OpenBusScriptedSystemPort([])
    port._bus = cast(MessageBus, new_bus)
    port._interface = cast(ProxyInterface, _FakeInterface(healthy=True))

    await port._disconnect(cast(MessageBus, old_bus))

    assert port._bus is new_bus
    assert new_bus.disconnect_calls == 0


class _LeakyMessageBus:
    """Stands in for :class:`dbus_fast.aio.MessageBus`: ``connect()`` succeeds, ``introspect()`` fails.

    Exercises the real (unstubbed) ``_open_bus``/``_open_systemd_bus``
    themselves -- see ``test_comitup_adapter.py``'s equivalent class for why.
    """

    instances: ClassVar[list[_LeakyMessageBus]] = []

    def __init__(self, *, bus_type: Any = None) -> None:
        self.disconnect_calls = 0
        _LeakyMessageBus.instances.append(self)

    async def connect(self) -> _LeakyMessageBus:
        return self

    async def introspect(self, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("logind is not on the bus")

    def disconnect(self) -> None:
        self.disconnect_calls += 1


async def test_open_bus_disconnects_a_connected_bus_when_introspect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _LeakyMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.systemd.MessageBus", _LeakyMessageBus)
    port = SystemdSystemPort()

    with pytest.raises(ConnectionRefusedError):
        await port._connect()

    assert len(_LeakyMessageBus.instances) == 1
    assert _LeakyMessageBus.instances[0].disconnect_calls == 1


async def test_open_systemd_bus_disconnects_a_connected_bus_when_introspect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _LeakyMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.systemd.MessageBus", _LeakyMessageBus)
    port = SystemdSystemPort()

    with pytest.raises(ConnectionRefusedError):
        await port._connect_systemd()

    assert len(_LeakyMessageBus.instances) == 1
    assert _LeakyMessageBus.instances[0].disconnect_calls == 1


def test_reboot_maps_introspect_failure_to_adapter_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _LeakyMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.systemd.MessageBus", _LeakyMessageBus)
    port = SystemdSystemPort()

    with pytest.raises(AdapterUnavailableError):
        port.reboot()

    assert len(_LeakyMessageBus.instances) == 2
    assert all(bus.disconnect_calls == 1 for bus in _LeakyMessageBus.instances)


class _HangingIntrospectMessageBus:
    """Stands in for :class:`dbus_fast.aio.MessageBus`: ``connect()`` succeeds, ``introspect()`` hangs forever.

    Exercises ``_connect()``/``_connect_systemd()``'s outer
    ``asyncio.wait_for(..., timeout=CONNECT_TIMEOUT_SECONDS)`` timing out
    and cancelling ``_open_bus()``/``_open_systemd_bus()`` mid-flight --
    see ``test_comitup_adapter.py``'s equivalent class for why this is a
    distinct case from ``_LeakyMessageBus`` above.
    """

    instances: ClassVar[list[_HangingIntrospectMessageBus]] = []

    def __init__(self, *, bus_type: Any = None) -> None:
        self.disconnect_calls = 0
        _HangingIntrospectMessageBus.instances.append(self)

    async def connect(self) -> _HangingIntrospectMessageBus:
        return self

    async def introspect(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)  # far longer than CONNECT_TIMEOUT_SECONDS below

    def disconnect(self) -> None:
        self.disconnect_calls += 1


async def test_connect_disconnects_the_bus_when_open_bus_is_cancelled_by_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _HangingIntrospectMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.systemd.MessageBus", _HangingIntrospectMessageBus)
    monkeypatch.setattr("palmimo_portal.adapters.systemd.CONNECT_TIMEOUT_SECONDS", 0.01)
    port = SystemdSystemPort()

    with pytest.raises(TimeoutError):
        await port._connect()

    assert len(_HangingIntrospectMessageBus.instances) == 1
    assert _HangingIntrospectMessageBus.instances[0].disconnect_calls == 1


async def test_connect_systemd_disconnects_the_bus_when_open_systemd_bus_is_cancelled_by_its_own_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _HangingIntrospectMessageBus.instances = []
    monkeypatch.setattr("palmimo_portal.adapters.systemd.MessageBus", _HangingIntrospectMessageBus)
    monkeypatch.setattr("palmimo_portal.adapters.systemd.CONNECT_TIMEOUT_SECONDS", 0.01)
    port = SystemdSystemPort()

    with pytest.raises(TimeoutError):
        await port._connect_systemd()

    assert len(_HangingIntrospectMessageBus.instances) == 1
    assert _HangingIntrospectMessageBus.instances[0].disconnect_calls == 1


async def test_a_timeout_is_never_retried() -> None:
    class _HangingInterface:
        def __init__(self) -> None:
            self.call_count = 0

        async def call_reboot(self, interactive: bool) -> Any:
            self.call_count += 1
            await asyncio.sleep(10)

    port = _OpenBusScriptedSystemPort([])
    port._bus = cast(MessageBus, _FakeBus("only"))
    interface = _HangingInterface()
    port._interface = cast(ProxyInterface, interface)

    with pytest.raises(AdapterUnavailableError):
        await port._call_resilient("reboot", (False,), 0.01)

    assert interface.call_count == 1
    assert port.open_bus_calls == 0
