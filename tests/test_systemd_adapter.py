"""CI-safe tests for :mod:`palmimo_portal.adapters.systemd`.

No real D-Bus connection: :meth:`SystemdSystemPort._call` is stubbed by
subclassing, the same pattern as ``test_comitup_adapter.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, cast

import pytest
from dbus_fast import Variant
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface
from dbus_fast.errors import DBusError

from palmimo_portal.adapters import systemd as systemd_module
from palmimo_portal.adapters.systemd import SystemdAppUnitPort, SystemdSyncUnitPort, SystemdSystemPort
from palmimo_portal.core.apps_start import derive_run_status
from palmimo_portal.ports import AdapterUnavailableError, PolkitDeniedError, UnitStatus


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


class _StubbedAppUnitPort(SystemdAppUnitPort):
    """Stubs `_call` directly -- for tests that only care what args reach it, not error translation."""

    def __init__(self, script: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script: dict[str, Any] = script or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def _call(self, member: str, args: tuple[Any, ...], *, unit: str, verb: str, **kwargs: Any) -> Any:
        self.calls.append((member, args))
        outcome = self.script.get(member, [None] * 10)
        if isinstance(outcome, list):
            outcome = outcome[len(self.calls) - 1] if len(self.calls) - 1 < len(outcome) else outcome[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _status_for_unit(self, unit: str) -> UnitStatus:
        return UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)


class _RawDBusInterface:
    """Stands in for the `ProxyInterface` `SystemdAppUnitPort._connect` returns -- one scripted `call_<member>`."""

    def __init__(self, member: str, outcome: Any) -> None:
        setattr(self, f"call_{member}", self._make_method(outcome))

    @staticmethod
    def _make_method(outcome: Any) -> Any:
        async def _method(*args: Any) -> Any:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return _method


class _DBusErrorAppUnitPort(SystemdAppUnitPort):
    """Stubs `_connect` (not `_call`) so `_call`'s own DBusError-to-PolkitDeniedError translation runs for real."""

    def __init__(self, member: str, outcome: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._interface = cast(ProxyInterface, _RawDBusInterface(member, outcome))
        self._bus = cast(MessageBus, _FakeBus("stub"))

    async def _connect(self) -> tuple[ProxyInterface, MessageBus]:
        assert self._interface is not None and self._bus is not None
        return self._interface, self._bus


def test_set_device_allow_resets_before_setting_camera_expands_to_three_groups() -> None:
    port = _StubbedAppUnitPort({"set_unit_properties": [None, None]})

    port.set_device_allow("teleop", ["char-video4linux", "char-media", "char-dma_heap"])

    assert [call[0] for call in port.calls] == ["set_unit_properties", "set_unit_properties"]
    reset_props, set_props = (call[1][2] for call in port.calls)
    assert reset_props == [("DeviceAllow", Variant("a(ss)", []))]
    assert set_props == [
        ("DeviceAllow", Variant("a(ss)", [("char-video4linux", "rw"), ("char-media", "rw"), ("char-dma_heap", "rw")]))
    ]


def test_set_device_allow_only_resets_when_no_devices_are_declared() -> None:
    port = _StubbedAppUnitPort({"set_unit_properties": [None]})

    port.set_device_allow("teleop", [])

    assert [call[0] for call in port.calls] == ["set_unit_properties"]


def test_start_calls_start_unit_with_replace_mode() -> None:
    port = _StubbedAppUnitPort({"start_unit": None})

    port.start("teleop")

    assert port.calls == [("start_unit", ("palmimo-app@teleop.service", "replace"))]


def test_stop_calls_stop_unit_with_replace_mode() -> None:
    port = _StubbedAppUnitPort({"stop_unit": None})

    port.stop("teleop")

    assert port.calls == [("stop_unit", ("palmimo-app@teleop.service", "replace"))]


def test_start_raises_polkit_denied_when_systemd_refuses_access() -> None:
    port = _DBusErrorAppUnitPort("start_unit", DBusError("org.freedesktop.DBus.Error.AccessDenied", "nope"))

    with pytest.raises(PolkitDeniedError) as excinfo:
        port.start("teleop")

    assert excinfo.value.unit == "palmimo-app@teleop.service"
    assert excinfo.value.verb == "start"


def test_start_raises_polkit_denied_on_interactive_authorization_required() -> None:
    port = _DBusErrorAppUnitPort(
        "start_unit", DBusError("org.freedesktop.DBus.Error.InteractiveAuthorizationRequired", "nope")
    )

    with pytest.raises(PolkitDeniedError):
        port.start("teleop")


def test_start_raises_adapter_unavailable_for_a_non_polkit_dbus_error() -> None:
    port = _DBusErrorAppUnitPort("start_unit", DBusError("org.freedesktop.DBus.Error.NoReply", "gone"))

    with pytest.raises(AdapterUnavailableError):
        port.start("teleop")


def test_status_reports_inactive_for_a_garbage_collected_template_instance() -> None:
    # systemd unloads a stopped template instance's in-memory unit object; a naive `GetUnit`
    # would surface that as a backend failure for every stopped app, not just a not-yet-started one.
    port = _DBusErrorAppUnitPort("load_unit", DBusError("org.freedesktop.systemd1.NoSuchUnit", "not loaded"))

    status = port.status("teleop")

    assert status.active_state == "inactive"
    assert status.sub_state == "dead"
    assert status.result == "success"
    assert status.exec_main_status == 0


class _PropertiesReadAppUnitPort(SystemdAppUnitPort):
    """Stubs the `LoadUnit` call and the properties `GetAll` round trip `status()` makes.

    `unit_properties`/`service_properties` stand in for what the Unit and Service D-Bus
    interfaces would each report for `GetAll` -- `status()` must merge both, since
    `Result`/`ExecMainStatus`/`ExecMainStartTimestamp` live only on the Service interface.
    """

    def __init__(
        self, unit_path: str, unit_properties: dict[str, Any], service_properties: dict[str, Any], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._unit_path = unit_path
        self._unit_properties = unit_properties
        self._service_properties = service_properties

    async def _call(self, member: str, args: tuple[Any, ...], *, unit: str, verb: str, **kwargs: Any) -> Any:
        assert member == "load_unit"
        return self._unit_path

    def _read_unit_properties(self, unit_path: str, *, unit: str) -> dict[str, Any]:
        assert unit_path == self._unit_path
        return {**self._unit_properties, **self._service_properties}


def test_status_reads_exec_main_status_from_the_service_interface() -> None:
    # ExecMainStatus/Result live on org.freedesktop.systemd1.Service, not .Unit -- reading only
    # the Unit interface silently drops them, and derive_run_status can no longer tell a crashed
    # app (needing repair) from one that merely exited cleanly.
    port = _PropertiesReadAppUnitPort(
        "/org/freedesktop/systemd1/unit/palmimo_2dapp_40teleop_2eservice",
        unit_properties={"ActiveState": "failed", "SubState": "failed"},
        service_properties={"Result": "exit-code", "ExecMainStatus": 78, "ExecMainStartTimestamp": 123.0},
    )

    status = port.status("teleop")

    assert derive_run_status(status).status == "needs_repair"


class _ScriptedStatusAppUnitPort(SystemdAppUnitPort):
    """Answers `_status_for_unit` from a fixed script, repeating the last entry once exhausted."""

    def __init__(self, statuses: list[UnitStatus], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._statuses = list(statuses)
        self.status_calls = 0

    def _status_for_unit(self, unit: str) -> UnitStatus:
        self.status_calls += 1
        index = min(self.status_calls - 1, len(self._statuses) - 1)
        return self._statuses[index]

    def _start_unit(self, unit: str) -> None:
        pass


def test_sync_unit_start_calls_start_unit_against_the_sync_template() -> None:
    port = SystemdSyncUnitPort()
    stub = _StubbedAppUnitPort({"start_unit": None})
    port._unit_port = stub

    port.start("jabc123")

    assert stub.calls == [("start_unit", ("palmimo-app-sync@jabc123.service", "replace"))]


def test_sync_unit_stop_calls_stop_unit_against_the_sync_template() -> None:
    port = SystemdSyncUnitPort()
    stub = _StubbedAppUnitPort({"stop_unit": None})
    port._unit_port = stub

    port.stop("jabc123")

    assert stub.calls == [("stop_unit", ("palmimo-app-sync@jabc123.service", "replace"))]


def test_sync_unit_wait_polls_until_the_unit_leaves_the_running_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(systemd_module, "_SYNC_POLL_INTERVAL_SECONDS", 0.0)
    running = UnitStatus(active_state="activating", sub_state="start", result="success", exec_main_status=0)
    done = UnitStatus(active_state="inactive", sub_state="dead", result="success", exec_main_status=0)
    port = SystemdSyncUnitPort()
    stub = _ScriptedStatusAppUnitPort([running, running, done])
    port._unit_port = stub

    status = port.wait("jabc123", timeout_s=5.0)

    assert status is done
    assert stub.status_calls == 3


def test_sync_unit_wait_observes_the_started_invocation_before_accepting_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemd_module, "_SYNC_POLL_INTERVAL_SECONDS", 0.0)
    previous = UnitStatus(
        active_state="inactive", sub_state="dead", result="success", exec_main_status=0, exec_main_start_timestamp=1.0
    )
    running = UnitStatus(
        active_state="activating",
        sub_state="start",
        result="success",
        exec_main_status=0,
        exec_main_start_timestamp=2.0,
    )
    failed = UnitStatus(
        active_state="inactive",
        sub_state="failed",
        result="exit-code",
        exec_main_status=1,
        exec_main_start_timestamp=2.0,
    )
    port = SystemdSyncUnitPort()
    stub = _ScriptedStatusAppUnitPort([previous, running, failed])
    port._unit_port = stub

    port.start("jabc123")
    status = port.wait("jabc123", timeout_s=5.0)

    assert status is failed
    assert stub.status_calls == 3


def test_sync_unit_wait_raises_timeout_error_when_still_running_past_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(systemd_module, "_SYNC_POLL_INTERVAL_SECONDS", 0.0)
    running = UnitStatus(active_state="activating", sub_state="start", result="success", exec_main_status=0)
    port = SystemdSyncUnitPort()
    port._unit_port = _ScriptedStatusAppUnitPort([running])

    with pytest.raises(TimeoutError):
        port.wait("jabc123", timeout_s=0.01)
