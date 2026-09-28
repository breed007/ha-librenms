"""Tests for LibreNMS diagnostics."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.coordinator import LibreNMSDevice
from custom_components.librenms.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import MockLibreNMS, async_poll, load_fixture_json, setup_integration
from .const import TOKEN

# Every field LibreNMS's device API returns that identifies the network, a
# person, or grants access. None of their values may appear in diagnostics.
SENSITIVE_DEVICE_FIELDS = (
    "hostname",
    "sysName",
    "display",
    "ip",
    "overwrite_ip",
    "community",
    "authname",
    "authpass",
    "cryptopass",
    "sysContact",
    "sysDescr",
    "snmpEngineID",
    "dependency_parent_hostname",
    "display_template",
    "bgpLocalAs",
    "purpose",
    "notes",
    "serial",
    "location",
    "lat",
    "lng",
    "features",
)


def _sensitive_values() -> list[str]:
    """Return every non-empty sensitive value in the device fixture."""
    values = []
    for device in load_fixture_json("devices.json")["devices"]:
        for field in SENSITIVE_DEVICE_FIELDS:
            value = device.get(field)
            if value not in (None, ""):
                values.append(str(value))
    return values


def _serialize(value: Any) -> str:
    return json.dumps(value, default=str)


def test_fixture_carries_the_sensitive_fields_it_claims_to() -> None:
    """Guard the guard: the leak test is only as good as its fixture."""
    devices = load_fixture_json("devices.json")["devices"]
    for field in (
        "community",
        "authpass",
        "cryptopass",
        "sysContact",
        "sysDescr",
        "snmpEngineID",
        "dependency_parent_hostname",
    ):
        assert any(device.get(field) for device in devices), field


async def test_diagnostics_leak_nothing_sensitive(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """No credential, contact, hostname or location value reaches the output."""
    await setup_integration(hass, mock_config_entry)

    serialized = _serialize(
        await async_get_config_entry_diagnostics(hass, mock_config_entry)
    )

    assert TOKEN not in serialized
    assert "librenms.example.com" not in serialized
    leaked = [value for value in _sensitive_values() if value in serialized]
    assert leaked == []
    # Alert free text can name hosts, so it is left out too.
    assert "ap-garage" not in serialized
    assert "Device down due to no ICMP response" not in serialized


# Device fields the integration never parses. None of their values may be
# held anywhere on the coordinator once a poll has been processed.
NEVER_RETAINED_FIELDS = (
    "community",
    "authname",
    "authpass",
    "cryptopass",
    "sysContact",
    "sysDescr",
    "snmpEngineID",
    "dependency_parent_hostname",
    "display_template",
    "purpose",
    "notes",
)


def _strings_held_by(root: object) -> list[str]:
    """Return every string reachable from `root` through this integration.

    Walks containers, dataclasses and any object whose class comes from
    custom_components, so a payload stashed on the coordinator, the client
    or anything they hold is found. Home Assistant's own objects (hass, the
    config entry, the HTTP session) are not entered; they are not ours to
    police, and hass reaches everything.
    """
    found: list[str] = []
    seen: set[int] = set()
    stack: list[object] = [root]
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, str):
            found.append(obj)
        elif isinstance(obj, bytes):
            found.append(obj.decode("utf-8", "replace"))
        elif isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
        elif type(obj).__module__.startswith("custom_components.") or (
            dataclasses.is_dataclass(obj) and not isinstance(obj, type)
        ):
            stack.extend(vars(obj).values() if hasattr(obj, "__dict__") else ())
            for cls in type(obj).__mro__:
                for slot in getattr(cls, "__slots__", ()):
                    if hasattr(obj, slot):
                        stack.append(getattr(obj, slot))
    return found


async def test_no_raw_payload_is_retained(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """SNMP secrets are dropped at parse time, not kept for the life of HA.

    Searches everything reachable from the coordinator, not just its parsed
    data, so a raw payload kept on the coordinator or the client is caught.
    """
    await setup_integration(hass, mock_config_entry)
    await async_poll(hass, freezer)
    coordinator = mock_config_entry.runtime_data

    assert "raw" not in {f.name for f in dataclasses.fields(LibreNMSDevice)}
    held = _strings_held_by(coordinator)
    # The walk must actually reach the parsed data, or it proves nothing.
    assert "core-sw01.lan.example" in held
    assert "Device down due to no ICMP response" in held

    secrets = [
        str(device[field])
        for device in load_fixture_json("devices.json")["devices"]
        for field in NEVER_RETAINED_FIELDS
        if device.get(field)
    ]
    assert secrets
    leaked = [secret for secret in secrets if any(secret in text for text in held)]
    assert leaked == []


async def test_diagnostics_keep_useful_context(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Enough survives to actually debug an issue."""
    await setup_integration(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["entry"] == {
        "url_scheme": "https",
        "verify_ssl": True,
        "options": {"scan_interval": 60, "include_disabled": False},
    }
    assert diagnostics["system"]["local_ver"] == "25.7.0"
    assert diagnostics["counts"] == {
        "devices_total": 2,
        "devices_up": 1,
        "devices_down": 1,
        "devices_excluded": 2,
        "alerts_active": 2,
        "alerts_critical": 1,
        "alerts_warning": 1,
        "alerts_critical_hidden": 0,
    }
    assert diagnostics["hidden_alerts"] == []
    assert len(diagnostics["devices"]) == 4
    core = diagnostics["devices"][0]
    assert core == {
        "device_id": 1,
        "up": True,
        "status_reason": None,
        "disabled": False,
        "ignored": False,
        "os": "edgeswitch",
        "vendor": "Ubiquiti",
        "hardware": "Ubiquiti EdgeSwitch 24",
        "version": "1.9.3",
        "uptime": 4321000,
        "last_polled": "2025-07-28 09:14:03",
        "last_ping": None,
        "alert_count": 1,
    }
    assert diagnostics["alerts"][0] == {
        "id": 101,
        "device_id": 2,
        "rule_id": 7,
        "severity": "critical",
        "state": 1,
        "acknowledged": False,
        "device_listed": True,
        "timestamp": "2025-07-28 09:12:00",
    }


async def test_unknown_system_fields_are_left_out(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A field a future LibreNMS adds to /system is not passed through blindly."""
    mock_librenms.system["system"][0]["future_field"] = "fx-future-value"
    await setup_integration(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert "fx-future-value" not in _serialize(diagnostics)


async def test_diagnostics_include_health_sensors(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Per-class counts, the implausible count and each reading are included."""
    await setup_integration(hass, mock_config_entry)

    sensors = (await async_get_config_entry_diagnostics(hass, mock_config_entry))[
        "sensors"
    ]

    # Fixture: deleted rows and `state` enums are not published at all.
    assert sensors["available"] is True
    assert sensors["total"] == 5
    assert sensors["by_class"] == {"fanspeed": 1, "temperature": 3, "voltage": 1}
    assert sensors["implausible"] == 1
    by_id = {sensor["sensor_id"]: sensor for sensor in sensors["sensors"]}
    assert by_id[13] == {
        "sensor_id": 13,
        "device_id": 1,
        "sensor_class": "voltage",
        "value": 3.299,
        "implausible": False,
        "limit_high": 3.6,
        "limit_low": 3.0,
    }
    assert by_id[21]["value"] is None
    assert by_id[21]["implausible"] is True
    # Descriptions are free text and stay out.
    assert "PSU 1" not in _serialize(sensors)


async def test_diagnostics_show_a_sensor_outage(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed sensors request is visible, not just an empty list."""
    await setup_integration(hass, mock_config_entry)
    mock_librenms.fail("sensors", status=500)
    await async_poll(hass, freezer)

    sensors = (await async_get_config_entry_diagnostics(hass, mock_config_entry))[
        "sensors"
    ]

    assert sensors["available"] is False
    assert sensors["total"] == 0


async def test_diagnostics_include_poller_health(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stalled poller shows up in diagnostics with how long it has been."""
    await setup_integration(hass, mock_config_entry)
    await async_poll(hass, freezer, seconds=901)

    poller = (await async_get_config_entry_diagnostics(hass, mock_config_entry))[
        "poller"
    ]

    assert poller["poller_stale"] is True
    assert poller["last_advanced"] == "2025-07-28 09:14:03"
    assert poller["stalled_for_seconds"] >= 900


async def test_diagnostics_list_hidden_alerts_without_names(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA round 3, F5: a hidden critical alert shows up by id only.

    Device 2 drops out of the list while its critical alert stays open.
    Diagnostics count it and list its ids, but its device name, hostname
    and rule name stay out like every other name.
    """
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["counts"]["alerts_critical_hidden"] == 1
    # Counted like any alert whose device is in Home Assistant, flagged.
    assert diagnostics["counts"]["alerts_active"] == 2
    assert diagnostics["counts"]["alerts_critical"] == 1
    hidden = {
        "id": 101,
        "device_id": 2,
        "rule_id": 7,
        "severity": "critical",
        "state": 1,
        "acknowledged": False,
        "device_listed": False,
        "timestamp": "2025-07-28 09:12:00",
    }
    assert diagnostics["hidden_alerts"] == [hidden]
    assert hidden in diagnostics["alerts"]
    serialized = _serialize(diagnostics)
    leaked = [value for value in _sensitive_values() if value in serialized]
    assert leaked == []
    assert "ap-garage" not in serialized
    assert "Device down due to no ICMP response" not in serialized
