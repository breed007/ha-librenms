"""Tests for LibreNMS diagnostics."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.coordinator import LibreNMSDevice
from custom_components.librenms.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import MockLibreNMS, load_fixture_json, setup_integration
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


async def test_no_raw_payload_is_retained(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """SNMP secrets are dropped at parse time, not kept for the life of HA."""
    await setup_integration(hass, mock_config_entry)

    assert "raw" not in {f.name for f in dataclasses.fields(LibreNMSDevice)}
    held = _serialize(dataclasses.asdict(mock_config_entry.runtime_data.data))
    for secret_field in ("community", "authpass", "cryptopass", "authname"):
        for device in load_fixture_json("devices.json")["devices"]:
            if device.get(secret_field):
                assert device[secret_field] not in held, secret_field


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
    }
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
        "alert_count": 1,
    }
    assert diagnostics["alerts"][0] == {
        "id": 101,
        "device_id": 2,
        "rule_id": 7,
        "severity": "critical",
        "state": 1,
        "acknowledged": False,
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
