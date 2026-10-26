"""Tests for :mod:`palmimo_portal.adapters.networkmanager`."""

from __future__ import annotations

import copy
import threading
from typing import Any

import pytest
from dbus_fast import Variant

from palmimo_portal.adapters.networkmanager import (
    ACTIVE_INTERFACE,
    CONNECTION_INTERFACE,
    NM_INTERFACE,
    NetworkManagerCallError,
    NetworkManagerSavedNetworkPort,
    Settings,
)
from palmimo_portal.ports import (
    AdapterUnavailableError,
    SavedNetwork,
    SavedNetworkActiveError,
    SavedNetworkNotFoundError,
    SavedNetworkOrderError,
    WifiDeviceStatus,
    WifiProfilePort,
)
from palmimo_portal.testing.fakes import FakeWifiProfilePort


def _wifi(ssid: str, *, priority: int | None = None, mode: str = "infrastructure", psk: bool = True) -> Settings:
    connection: dict[str, Variant] = {"id": Variant("s", ssid), "type": Variant("s", "802-11-wireless")}
    if priority is not None:
        connection["autoconnect-priority"] = Variant("i", priority)
    settings: Settings = {
        "connection": connection,
        "802-11-wireless": {"ssid": Variant("ay", ssid.encode()), "mode": Variant("s", mode)},
    }
    if psk:
        settings["802-11-wireless-security"] = {"key-mgmt": Variant("s", "wpa-psk")}
    return settings


class _FakeNetworkManager(NetworkManagerSavedNetworkPort):
    """Answers :meth:`_nm_call` from memory: ``profiles`` maps object path to settings."""

    def __init__(
        self, profiles: dict[str, Settings], active: set[str] | None = None, *, edit_lock: threading.RLock | None = None
    ) -> None:
        super().__init__(edit_lock=edit_lock)
        self.profiles = profiles
        self.active = set(active or ())
        self.updates: list[tuple[str, Settings]] = []
        self.activations: list[str] = []
        self.vanish_on_get: set[str] = set()
        self.deny_get: set[str] = set()
        self._next = 100

    async def _nm_call(
        self, path: str, interface: str, member: str, signature: str = "", body: list[Any] | None = None
    ) -> list[Any]:
        body = body or []
        if member == "ListConnections":
            return [list(self.profiles)]
        if member == "GetSettings":
            if path in self.vanish_on_get or path not in self.profiles:
                raise NetworkManagerCallError(member, "org.freedesktop.DBus.Error.UnknownMethod", "gone")
            if path in self.deny_get:
                raise NetworkManagerCallError(member, "org.freedesktop.NetworkManager.PermissionDenied", "denied")
            settings = copy.deepcopy(self.profiles[path])
            settings.get("802-11-wireless-security", {}).pop("psk", None)
            return [settings]
        if member == "AddConnection":
            new_path = f"/Settings/{self._next}"
            self._next += 1
            self.profiles[new_path] = copy.deepcopy(body[0])
            return [new_path]
        if member == "Update":
            assert interface == CONNECTION_INTERFACE
            self.updates.append((path, body[0]))
            updated = copy.deepcopy(body[0])
            security = updated.get("802-11-wireless-security")
            stored_psk = self.profiles[path].get("802-11-wireless-security", {}).get("psk")
            if security is not None and "psk" not in security and stored_psk is not None:
                security["psk"] = stored_psk
            self.profiles[path] = updated
            return []
        if member == "Delete":
            del self.profiles[path]
            return []
        if member == "Get" and body == [NM_INTERFACE, "ActiveConnections"]:
            return [Variant("ao", [f"/Active{p}" for p in self.active])]
        if member == "Get" and body[0] == ACTIVE_INTERFACE:
            return [Variant("o", path.removeprefix("/Active"))]
        if member == "ActivateConnection":
            self.activations.append(body[0])
            return ["/Active/1"]
        raise AssertionError(f"unexpected call {member} {body}")


def _priority(settings: Settings) -> int:
    return int(settings["connection"]["autoconnect-priority"].value)


def test_list_saved_orders_by_priority_and_skips_the_setup_ap_and_ethernet() -> None:
    port = _FakeNetworkManager(
        {
            "/Settings/1": _wifi("office"),
            "/Settings/2": _wifi("palmimo-0001", mode="ap", priority=100),
            "/Settings/3": _wifi("phone", priority=20),
            "/Settings/4": {"connection": {"type": Variant("s", "802-3-ethernet")}},
        },
        active={"/Settings/1"},
    )

    assert port.list_saved() == [
        SavedNetwork(ssid="phone", priority=20, active=False),
        SavedNetwork(ssid="office", priority=0, active=True),
    ]


def test_list_saved_skips_a_profile_deleted_since_list_connections() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office"), "/Settings/2": _wifi("gone")})
    port.vanish_on_get.add("/Settings/2")

    assert [item.ssid for item in port.list_saved()] == ["office"]


def test_list_saved_reports_a_failure_other_than_a_vanished_profile() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office")})
    port.deny_get.add("/Settings/1")

    with pytest.raises(AdapterUnavailableError):
        port.list_saved()


def test_save_first_adds_a_new_profile_above_every_other() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office", priority=5)})

    port.save("cafe", "secret123", first=True)

    added = port.profiles["/Settings/100"]
    assert _priority(added) == 6
    assert added["802-11-wireless-security"]["psk"].value == "secret123"
    assert bytes(added["802-11-wireless"]["ssid"].value) == b"cafe"


def test_save_not_first_adds_a_new_profile_below_every_other() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office", priority=0)})

    port.save("cafe", "secret123", first=False)

    assert _priority(port.profiles["/Settings/100"]) == -1


def test_save_an_open_network_writes_no_security_setting() -> None:
    port = _FakeNetworkManager({})

    port.save("free", "", first=False)

    assert "802-11-wireless-security" not in port.profiles["/Settings/100"]


def test_save_an_existing_network_replaces_its_psk_in_place() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office", priority=3)})

    port.save("office", "newsecret", first=False)

    assert list(port.profiles) == ["/Settings/1"]
    updated = port.profiles["/Settings/1"]
    assert updated["802-11-wireless-security"]["psk"].value == "newsecret"
    assert _priority(updated) == 3


def test_save_an_existing_wpa3_network_keeps_it_sae() -> None:
    settings = _wifi("office")
    settings["802-11-wireless-security"]["key-mgmt"] = Variant("s", "sae")
    port = _FakeNetworkManager({"/Settings/1": settings})

    port.save("office", "newsecret", first=False)

    assert port.profiles["/Settings/1"]["802-11-wireless-security"]["key-mgmt"].value == "sae"


def test_activate_picks_the_listed_profile_when_an_ssid_has_duplicates() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office", priority=0), "/Settings/2": _wifi("office", priority=5)})

    port.activate("office")

    assert port.activations == ["/Settings/2"]


def test_delete_removes_the_profile() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office"), "/Settings/2": _wifi("cafe")}, active={"/Settings/1"})

    port.delete("cafe")

    assert list(port.profiles) == ["/Settings/1"]


def test_delete_refuses_the_active_network() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office")}, active={"/Settings/1"})

    with pytest.raises(SavedNetworkActiveError):
        port.delete("office")
    assert list(port.profiles) == ["/Settings/1"]


def test_delete_raises_not_found_for_an_unknown_ssid() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office")})

    with pytest.raises(SavedNetworkNotFoundError):
        port.delete("nowhere")


def test_reorder_writes_descending_priorities_and_skips_unchanged_profiles() -> None:
    port = _FakeNetworkManager(
        {
            "/Settings/1": _wifi("office", priority=2),
            "/Settings/2": _wifi("phone", priority=1),
            "/Settings/3": _wifi("palmimo-0001", mode="ap", priority=100),
        }
    )

    port.reorder(["phone", "office"])

    assert _priority(port.profiles["/Settings/2"]) == 2
    assert _priority(port.profiles["/Settings/1"]) == 1
    assert _priority(port.profiles["/Settings/3"]) == 100
    assert [item.ssid for item in port.list_saved()] == ["phone", "office"]


def test_reorder_sends_the_settings_back_without_adding_secrets() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office"), "/Settings/2": _wifi("phone")})

    port.reorder(["phone", "office"])

    for _, settings in port.updates:
        assert "psk" not in settings.get("802-11-wireless-security", {})


def test_reorder_rejects_a_list_that_is_not_exactly_the_saved_set() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office"), "/Settings/2": _wifi("phone")})

    with pytest.raises(SavedNetworkOrderError):
        port.reorder(["office"])
    with pytest.raises(SavedNetworkOrderError):
        port.reorder(["office", "phone", "phone"])
    assert port.updates == []


def test_activate_asks_networkmanager_to_activate_the_profile() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office"), "/Settings/2": _wifi("phone")})

    port.activate("phone")

    assert port.activations == ["/Settings/2"]


def test_activate_raises_not_found_for_an_unknown_ssid() -> None:
    port = _FakeNetworkManager({"/Settings/1": _wifi("office")})

    with pytest.raises(SavedNetworkNotFoundError):
        port.activate("nowhere")


@pytest.fixture(params=["networkmanager", "fake"])
def profile_port(request: pytest.FixtureRequest) -> WifiProfilePort:
    return _FakeNetworkManager({}) if request.param == "networkmanager" else FakeWifiProfilePort()


def test_list_profiles_preserves_raw_ssids_metadata_and_duplicates() -> None:
    settings = _wifi("office")
    settings["connection"].update({"uuid": Variant("s", "one"), "autoconnect": Variant("b", False)})
    settings["802-11-wireless"]["ssid"] = Variant("ay", b"\xff\xfeAB")
    settings["user"] = {"data": Variant("a{ss}", {"owner": "portal"})}
    duplicate = copy.deepcopy(settings)
    duplicate["connection"]["uuid"] = Variant("s", "two")
    port = _FakeNetworkManager(
        {
            "/1": settings,
            "/2": duplicate,
            "/ap": _wifi("ap", mode="ap"),
            "/wired": {"connection": {"type": Variant("s", "802-3-ethernet")}},
        }
    )

    profiles = port.list_profiles()

    assert [(p.uuid, p.name, p.ssid, p.priority, p.autoconnect, p.user_data) for p in profiles] == [
        (uid, "office", b"\xff\xfeAB", 0, False, {"owner": "portal"}) for uid in ("one", "two")
    ]


@pytest.mark.parametrize("psk", ["", "secret123"])
def test_add_profile_creates_an_independent_persistent_profile(psk: str) -> None:
    port = _FakeNetworkManager({})
    uid = port.add_profile(b"\xffAB", psk, name="trial", priority=999, user_data={"attempt": "id"})
    profile = port.list_profiles()[0]
    assert (profile.uuid, profile.ssid, profile.name, profile.priority, profile.autoconnect, profile.user_data) == (
        uid,
        b"\xffAB",
        "trial",
        999,
        True,
        {"attempt": "id"},
    )
    settings = port.profiles["/Settings/100"]
    assert settings["ipv4"]["method"].value == settings["ipv6"]["method"].value == "auto"
    assert settings.get("802-11-wireless-security", {}).get("psk", Variant("s", "")).value == psk
    if not psk:
        assert "802-11-wireless-security" not in settings


def test_update_profile_changes_only_supplied_fields_and_can_clear_metadata(profile_port: WifiProfilePort) -> None:
    port = profile_port
    uid = port.add_profile(b"office", "secret123", name="office", priority=2, user_data={"owner": "other"})
    port.update_profile(uid, ssid=b"parked", autoconnect=False)
    parked = port.list_profiles()[0]
    assert (parked.ssid, parked.autoconnect, parked.priority, parked.name, parked.user_data) == (
        b"parked",
        False,
        2,
        "office",
        {"owner": "other"},
    )
    port.update_profile(uid, ssid=b"office", autoconnect=True, priority=-999, name="restored", user_data={})
    restored = port.list_profiles()[0]
    assert (restored.ssid, restored.autoconnect, restored.priority, restored.name, restored.user_data) == (
        b"office",
        True,
        -999,
        "restored",
        {},
    )


def test_delete_profile_allows_an_active_uuid(profile_port: WifiProfilePort) -> None:
    port = profile_port
    uid = port.add_profile(b"office", "", name="office", priority=0, user_data={})
    if isinstance(port, FakeWifiProfilePort):
        port.device = WifiDeviceStatus(100, uid)
    else:
        assert isinstance(port, _FakeNetworkManager)
        port.active.add("/Settings/100")
    port.delete_profile(uid)
    assert port.list_profiles() == []


@pytest.mark.parametrize("operation", ["update_profile", "delete_profile"])
def test_profile_edit_reports_missing_uuid(operation: str, profile_port: WifiProfilePort) -> None:
    port = profile_port
    with pytest.raises(SavedNetworkNotFoundError):
        getattr(port, operation)("missing")


@pytest.mark.parametrize("active_uuid", [None, "ap-profile", "office-profile"])
def test_device_status_reads_wlan0_and_disconnects(active_uuid: str | None) -> None:
    class DeviceNM(_FakeNetworkManager):
        state = 100
        active_uuid_value = active_uuid

        async def _nm_call(
            self, path: str, interface: str, member: str, signature: str = "", body: list[Any] | None = None
        ) -> list[Any]:
            if member == "GetDeviceByIpIface":
                return ["/Devices/wlan0"]
            if member == "Disconnect":
                self.state = 30
                self.active_uuid_value = None
                return []
            if member == "Get" and body:
                if body[1] == "State":
                    return [Variant("u", self.state)]
                if body[1] == "ActiveConnection":
                    return [Variant("o", "/Active/1" if self.active_uuid_value else "/")]
                if body[1] == "Uuid":
                    return [Variant("s", self.active_uuid_value)]
            return await super()._nm_call(path, interface, member, signature, body)

    port = DeviceNM({})
    assert port.device_status() == WifiDeviceStatus(100, active_uuid)
    port.disconnect_device()
    assert port.device_status() == WifiDeviceStatus(30, None)


def test_fake_profile_restore_preserves_password() -> None:
    port = FakeWifiProfilePort()
    uid = port.add_profile(b"\xffoffice", "secret123", name="office", priority=10, user_data={})
    port.update_profile(uid, ssid=b"parked", autoconnect=False)
    port.update_profile(uid, ssid=b"\xffoffice", autoconnect=True)
    assert port.psks[uid] == "secret123"
    assert port.list_profiles()[0].ssid == b"\xffoffice"
    assert port.list_profiles()[0].autoconnect


def test_profile_creation_keeps_duplicate_ssids_and_isolates_metadata(profile_port: WifiProfilePort) -> None:
    data = {"owner": "portal"}
    first = profile_port.add_profile(b"\xffoffice", "secret123", name="office", priority=2, user_data=data)
    second = profile_port.add_profile(b"\xffoffice", "", name="trial", priority=999, user_data=data)
    data.clear()
    profiles = profile_port.list_profiles()
    assert first != second
    assert [(p.uuid, p.ssid, p.user_data) for p in profiles] == [
        (uid, b"\xffoffice", {"owner": "portal"}) for uid in (first, second)
    ]
    profiles[0].user_data.clear()
    assert profile_port.list_profiles()[0].user_data == {"owner": "portal"}


def test_fake_device_disconnect_clears_active_uuid() -> None:
    port = FakeWifiProfilePort(device=WifiDeviceStatus(100, "active"))
    assert port.device_status() == WifiDeviceStatus(100, "active")
    port.disconnect_device()
    assert port.device_status() == WifiDeviceStatus(30, None)


def test_shared_profile_edit_lock_blocks_saved_network_writes() -> None:
    edit_lock = threading.RLock()
    port = _FakeNetworkManager({}, edit_lock=edit_lock)
    entered = threading.Event()
    finished = threading.Event()

    def save() -> None:
        entered.set()
        port.save("other", "password", first=False)
        finished.set()

    with edit_lock:
        worker = threading.Thread(target=save)
        worker.start()
        assert entered.wait(2)
        assert not finished.wait(0.05)
        assert port.profiles == {}
    assert finished.wait(2)
    worker.join(2)
    assert port.list_saved()[0].ssid == "other"
