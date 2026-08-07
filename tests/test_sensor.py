"""Tests for the LibreNMS sensor and binary sensor platforms."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from .conftest import MockLibreNMS, async_poll, setup_integration


async def test_instance_sensors(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Summary sensors reflect the fixture instance."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("sensor.librenms_devices").state == "2"
    assert hass.states.get("sensor.librenms_devices_up").state == "1"
    assert hass.states.get("sensor.librenms_devices_down").state == "1"
    assert hass.states.get("sensor.librenms_active_alerts").state == "2"
    assert hass.states.get("sensor.librenms_critical_alerts").state == "1"
    assert hass.states.get("sensor.librenms_warning_alerts").state == "1"


async def test_active_alerts_attributes(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The alert list is exposed as attributes, most severe first."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get("sensor.librenms_active_alerts")
    alerts = state.attributes["alerts"]

    assert state.attributes["truncated"] is False
    assert [alert["severity"] for alert in alerts] == ["critical", "warning"]
    assert alerts[0] == {
        "id": 101,
        "device_id": 2,
        "hostname": "ap-garage.lan.example",
        "rule_id": 7,
        "rule": "Device down due to no ICMP response",
        "severity": "critical",
        "timestamp": "2025-07-28 09:12:00",
        "note": None,
    }


async def test_active_alerts_attributes_are_capped(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A busy instance does not push an unbounded list into the recorder."""
    mock_librenms.set_alerts(
        [
            {
                "id": str(200 + index),
                "device_id": "1",
                "hostname": "core-sw01.lan.example",
                "rule_id": "12",
                "name": "Noisy rule",
                "severity": "warning",
                "timestamp": "2025-07-28 09:00:00",
            }
            for index in range(75)
        ]
    )
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get("sensor.librenms_active_alerts")
    assert state.state == "75"
    assert len(state.attributes["alerts"]) == 50
    assert state.attributes["truncated"] is True


async def test_device_sensors(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Per-device sensors report alert counts and boot time."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("sensor.core_sw01_active_alerts").state == "1"
    assert hass.states.get("sensor.garage_ap_active_alerts").state == "1"

    boot = hass.states.get("sensor.core_sw01_last_boot")
    expected = dt_util.utcnow() - timedelta(seconds=4321000)
    assert abs(dt_util.parse_datetime(boot.state) - expected) < timedelta(seconds=2)

    # A device that is down has no meaningful boot time.
    assert hass.states.get("sensor.garage_ap_last_boot").state == "unknown"


async def test_boot_time_is_stable_across_polls(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Poll jitter must not rewrite the boot time on every update."""
    await setup_integration(hass, mock_config_entry)
    first = hass.states.get("sensor.core_sw01_last_boot").state

    devices = list(mock_librenms.devices["devices"])
    # 61s later the device reports 61s more uptime, plus a second of jitter.
    devices[0] = {**devices[0], "uptime": 4321000 + 62}
    mock_librenms.set_devices(devices)

    await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_last_boot").state == first


async def test_boot_time_moves_after_a_reboot(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A genuine reboot resets the reported boot time."""
    await setup_integration(hass, mock_config_entry)
    first = hass.states.get("sensor.core_sw01_last_boot").state

    devices = list(mock_librenms.devices["devices"])
    devices[0] = {**devices[0], "uptime": 30}
    mock_librenms.set_devices(devices)

    await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_last_boot").state != first


async def test_diagnostic_sensors_are_disabled_by_default(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Low-value sensors are registered but not enabled."""
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)

    for entity_id in (
        "sensor.core_sw01_hardware",
        "sensor.core_sw01_operating_system",
        "sensor.core_sw01_last_polled",
        "sensor.librenms_devices_excluded",
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None


async def test_binary_sensors(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Connectivity and problem states follow the fixture instance."""
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"

    garage = hass.states.get("binary_sensor.garage_ap_status")
    assert garage.state == "off"
    assert garage.attributes["status_reason"] == "icmp"
    assert garage.attributes["hostname"] == "ap-garage.lan.example"

    problem = hass.states.get("binary_sensor.librenms_problem")
    assert problem.state == "on"
    assert problem.attributes["devices_down"] == 1
    assert problem.attributes["alerts_critical"] == 1
    assert problem.attributes["down_hostnames"] == ["ap-garage.lan.example"]


async def test_problem_clears(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The problem sensor turns off once everything is up and quiet."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.librenms_problem").state == "on"

    devices = [
        {**device, "status": 1, "status_reason": ""}
        for device in mock_librenms.devices["devices"]
    ]
    mock_librenms.set_devices(devices)
    mock_librenms.set_alerts([])

    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.librenms_problem").state == "off"
