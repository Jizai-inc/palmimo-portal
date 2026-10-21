"""Real :class:`~palmimo_portal.ports.SavedNetworkPort`: NetworkManager's settings over D-Bus.

comitup has no call to list or reorder saved networks, so this adapter edits
NetworkManager's infrastructure Wi-Fi profiles directly. The device image's
polkit rules grant the Portal's account ``settings.modify.system`` and
``network-control`` for it.

**Secrets survive** ``Update``: NetworkManager keeps a profile's stored
secrets when an update carries none, so reordering sends the settings from
``GetSettings`` (which never include the passphrase) as they are.

**Switching keeps the old network.** :meth:`NetworkManagerSavedNetworkPort.activate`
asks NetworkManager to activate the profile; comitup sees the active SSID
change and settles on it, since the new SSID is one of its candidates.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from palmimo_portal.adapters.dbus_support import SharedEventLoopThread, get_shared_loop_thread
from palmimo_portal.ports import (
    AdapterUnavailableError,
    SavedNetwork,
    SavedNetworkActiveError,
    SavedNetworkNotFoundError,
    SavedNetworkOrderError,
    SavedNetworkPort,
)


logger = logging.getLogger("palmimo_portal")

NM_BUS_NAME = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
NM_INTERFACE = "org.freedesktop.NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
SETTINGS_INTERFACE = "org.freedesktop.NetworkManager.Settings"
CONNECTION_INTERFACE = "org.freedesktop.NetworkManager.Settings.Connection"
ACTIVE_INTERFACE = "org.freedesktop.NetworkManager.Connection.Active"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

CALL_TIMEOUT_SECONDS = 10.0
#: NetworkManager's own bounds for connection.autoconnect-priority.
MAX_PRIORITY = 999
MIN_PRIORITY = -999

_ADAPTER_ERROR_CODE = "network_backend_unavailable"

#: D-Bus errors meaning the object went away between two calls (a profile
#: deleted, a connection deactivated) -- skipped, not reported.
_GONE_ERRORS = frozenset({"org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.DBus.Error.UnknownMethod"})

Settings = dict[str, dict[str, Variant]]


class NetworkManagerCallError(AdapterUnavailableError):
    """A NetworkManager method returned a D-Bus error."""

    def __init__(self, member: str, error_name: str | None, detail: Any) -> None:
        super().__init__(_ADAPTER_ERROR_CODE, f"NetworkManager {member}() failed: {error_name}: {detail}")
        self.error_name = error_name

    @property
    def gone(self) -> bool:
        return self.error_name in _GONE_ERRORS


def _ssid_of(settings: Settings) -> str | None:
    wireless = settings.get("802-11-wireless", {})
    if "ssid" not in wireless:
        return None
    return bytes(wireless["ssid"].value).decode("utf-8", errors="replace")


def _is_infrastructure(settings: Settings) -> bool:
    connection = settings.get("connection", {})
    wireless = settings.get("802-11-wireless", {})
    connection_type = connection["type"].value if "type" in connection else None
    mode = wireless["mode"].value if "mode" in wireless else "infrastructure"
    return connection_type == "802-11-wireless" and mode == "infrastructure"


def _priority_of(settings: Settings) -> int:
    connection = settings.get("connection", {})
    return int(connection["autoconnect-priority"].value) if "autoconnect-priority" in connection else 0


def _with_priority(settings: Settings, priority: int) -> Settings:
    updated = {group: dict(values) for group, values in settings.items()}
    updated["connection"]["autoconnect-priority"] = Variant("i", priority)
    return updated


def _new_profile(ssid: str, psk: str, priority: int) -> Settings:
    profile: Settings = {
        "connection": {
            "id": Variant("s", ssid),
            "type": Variant("s", "802-11-wireless"),
            "uuid": Variant("s", str(uuid.uuid4())),
            "autoconnect-priority": Variant("i", priority),
        },
        "802-11-wireless": {
            "ssid": Variant("ay", ssid.encode("utf-8")),
            "mode": Variant("s", "infrastructure"),
        },
        "ipv4": {"method": Variant("s", "auto")},
        "ipv6": {"method": Variant("s", "auto")},
    }
    if psk:
        profile["802-11-wireless"]["security"] = Variant("s", "802-11-wireless-security")
        profile["802-11-wireless-security"] = {
            "key-mgmt": Variant("s", "wpa-psk"),
            "psk": Variant("s", psk),
        }
    return profile


def _with_psk(settings: Settings, psk: str) -> Settings:
    updated = {group: dict(values) for group, values in settings.items()}
    if psk:
        updated["802-11-wireless"]["security"] = Variant("s", "802-11-wireless-security")
        updated["802-11-wireless-security"] = {
            "key-mgmt": Variant("s", "wpa-psk"),
            "psk": Variant("s", psk),
        }
    else:
        updated["802-11-wireless"].pop("security", None)
        updated.pop("802-11-wireless-security", None)
    return updated


class NetworkManagerSavedNetworkPort(SavedNetworkPort):
    """Edits NetworkManager's saved Wi-Fi profiles. See the module docstring.

    Every public method holds :attr:`_edit_lock` across its whole
    read-modify-write, so two requests reordering at once cannot interleave.
    :meth:`_nm_call` is the single D-Bus seam tests stub.
    """

    def __init__(self, *, loop_thread: SharedEventLoopThread | None = None) -> None:
        self._loop_thread = loop_thread if loop_thread is not None else get_shared_loop_thread()
        self._bus: MessageBus | None = None
        self._bus_lock = asyncio.Lock()
        self._edit_lock = asyncio.Lock()

    async def _get_bus(self) -> MessageBus:
        async with self._bus_lock:
            if self._bus is None or not self._bus.connected:
                self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            return self._bus

    async def _nm_call(
        self, path: str, interface: str, member: str, signature: str = "", body: list[Any] | None = None
    ) -> list[Any]:
        bus = await self._get_bus()
        reply = await bus.call(
            Message(
                destination=NM_BUS_NAME,
                path=path,
                interface=interface,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        assert reply is not None
        if reply.message_type is MessageType.ERROR:
            raise NetworkManagerCallError(member, reply.error_name, reply.body)
        return list(reply.body)

    def _run(self, coro: Any) -> Any:
        try:
            return self._loop_thread.run(coro, timeout=CALL_TIMEOUT_SECONDS)
        except (AdapterUnavailableError, SavedNetworkNotFoundError, SavedNetworkActiveError, SavedNetworkOrderError):
            raise
        except Exception as error:
            raise AdapterUnavailableError(_ADAPTER_ERROR_CODE, f"NetworkManager call failed: {error}") from error

    async def _profiles(self) -> list[tuple[str, Settings]]:
        """Return ``(path, settings)`` for every infrastructure Wi-Fi profile, in NetworkManager's order."""
        (paths,) = await self._nm_call(SETTINGS_PATH, SETTINGS_INTERFACE, "ListConnections")
        profiles = []
        for path in paths:
            try:
                (settings,) = await self._nm_call(path, CONNECTION_INTERFACE, "GetSettings")
            except NetworkManagerCallError as error:
                if not error.gone:
                    raise
                continue  # deleted since ListConnections
            if _is_infrastructure(settings) and _ssid_of(settings):
                profiles.append((path, settings))
        return profiles

    async def _active_paths(self) -> set[str]:
        (variant,) = await self._nm_call(
            NM_PATH, PROPERTIES_INTERFACE, "Get", "ss", [NM_INTERFACE, "ActiveConnections"]
        )
        paths = set()
        for active in variant.value:
            try:
                (connection,) = await self._nm_call(
                    active, PROPERTIES_INTERFACE, "Get", "ss", [ACTIVE_INTERFACE, "Connection"]
                )
            except NetworkManagerCallError as error:
                if not error.gone:
                    raise
                continue  # deactivated meanwhile
            paths.add(connection.value)
        return paths

    @staticmethod
    def _ordered(profiles: list[tuple[str, Settings]]) -> list[tuple[str, Settings]]:
        """Highest priority first; ties keep NetworkManager's order (the order comitup tries them in)."""
        return sorted(profiles, key=lambda item: -_priority_of(item[1]))

    @staticmethod
    def _find(profiles: list[tuple[str, Settings]], ssid: str) -> tuple[str, Settings]:
        for path, settings in profiles:
            if _ssid_of(settings) == ssid:
                return path, settings
        raise SavedNetworkNotFoundError(ssid)

    async def _list_saved(self) -> list[SavedNetwork]:
        profiles = self._ordered(await self._profiles())
        active = await self._active_paths()
        listed: list[SavedNetwork] = []
        seen: set[str] = set()
        for path, settings in profiles:
            ssid = _ssid_of(settings)
            assert ssid is not None
            if ssid in seen:
                continue  # a duplicate profile for one SSID: comitup only ever uses the first
            seen.add(ssid)
            listed.append(SavedNetwork(ssid=ssid, priority=_priority_of(settings), active=path in active))
        return listed

    def list_saved(self) -> list[SavedNetwork]:
        async def run() -> list[SavedNetwork]:
            async with self._edit_lock:
                return await self._list_saved()

        result: list[SavedNetwork] = self._run(run())
        return result

    async def _save(self, ssid: str, psk: str, first: bool) -> None:
        profiles = await self._profiles()
        priorities = [_priority_of(settings) for _, settings in profiles]
        try:
            path, settings = self._find(profiles, ssid)
        except SavedNetworkNotFoundError:
            if first:
                priority = min(max(priorities, default=0) + 1, MAX_PRIORITY)
            else:
                priority = max(min(priorities, default=1) - 1, MIN_PRIORITY)
            await self._nm_call(
                SETTINGS_PATH, SETTINGS_INTERFACE, "AddConnection", "a{sa{sv}}", [_new_profile(ssid, psk, priority)]
            )
            logger.info("saved network %r added at priority %d", ssid, priority)
            return
        updated = _with_psk(settings, psk)
        if first:
            others = [_priority_of(s) for p, s in profiles if p != path]
            updated = _with_priority(updated, min(max(others, default=0) + 1, MAX_PRIORITY))
        await self._nm_call(path, CONNECTION_INTERFACE, "Update", "a{sa{sv}}", [updated])
        logger.info("saved network %r updated", ssid)

    def save(self, ssid: str, psk: str, *, first: bool) -> None:
        async def run() -> None:
            async with self._edit_lock:
                await self._save(ssid, psk, first)

        self._run(run())

    async def _delete(self, ssid: str) -> None:
        profiles = await self._profiles()
        active = await self._active_paths()
        matches = [path for path, settings in profiles if _ssid_of(settings) == ssid]
        if not matches:
            raise SavedNetworkNotFoundError(ssid)
        if any(path in active for path in matches):
            raise SavedNetworkActiveError(ssid)
        for path in matches:
            await self._nm_call(path, CONNECTION_INTERFACE, "Delete")
        logger.info("saved network %r deleted", ssid)

    def delete(self, ssid: str) -> None:
        async def run() -> None:
            async with self._edit_lock:
                await self._delete(ssid)

        self._run(run())

    async def _reorder(self, ssids: list[str]) -> None:
        profiles = self._ordered(await self._profiles())
        saved = list(dict.fromkeys(_ssid_of(settings) for _, settings in profiles))
        if len(ssids) != len(set(ssids)) or set(ssids) != set(saved):
            raise SavedNetworkOrderError(f"expected exactly {saved}, got {ssids}")
        for index, ssid in enumerate(ssids):
            priority = len(ssids) - index
            for path, settings in profiles:
                if _ssid_of(settings) == ssid and _priority_of(settings) != priority:
                    await self._nm_call(
                        path, CONNECTION_INTERFACE, "Update", "a{sa{sv}}", [_with_priority(settings, priority)]
                    )
        logger.info("saved networks reordered: %s", ssids)

    def reorder(self, ssids: list[str]) -> None:
        async def run() -> None:
            async with self._edit_lock:
                await self._reorder(ssids)

        self._run(run())

    async def _activate(self, ssid: str) -> None:
        path, _ = self._find(await self._profiles(), ssid)
        await self._nm_call(NM_PATH, NM_INTERFACE, "ActivateConnection", "ooo", [path, "/", "/"])
        logger.info("switching to saved network %r", ssid)

    def activate(self, ssid: str) -> None:
        async def run() -> None:
            async with self._edit_lock:
                await self._activate(ssid)

        self._run(run())
