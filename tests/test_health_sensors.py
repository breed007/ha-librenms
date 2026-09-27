"""Tests for LibreNMS health sensors."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.coordinator import LibreNMSSensor

from .conftest import MockLibreNMS, async_poll, setup_integration


def test_scaling_is_not_reapplied() -> None:
    """`sensor_current` is already scaled; divisor/multiplier are history.

    A real instance reports 3.299 V with divisor 1000. Dividing again would
    publish 0.0033 V, which looks plausible enough to go unnoticed.
    """
    sensor = LibreNMSSensor.from_api(
        {
            "sensor_id": 1,
            "device_id": 1,
            "sensor_class": "voltage",
            "sensor_current": 3.299,
            "sensor_divisor": 1000,
            "sensor_multiplier": 1,
        }
    )
    assert sensor is not None
    assert sensor.value == 3.299


def test_sentinel_readings_are_rejected() -> None:
    """Hardware with no reading returns a 32-bit sentinel, not nothing."""
    sensor = LibreNMSSensor.from_api(
        {
            "sensor_id": 1,
            "device_id": 1,
            "sensor_class": "temperature",
            "sensor_current": 4294704.096,
            "sensor_limit": 58,
        }
    )
    assert sensor is not None
    assert sensor.value is None
    assert sensor.implausible is True


def test_deleted_and_enum_sensors_are_skipped() -> None:
    """Retired rows and untranslatable state enums are not published."""
    base = {"sensor_id": 1, "device_id": 1, "sensor_current": 1}
    assert (
        LibreNMSSensor.from_api(
            {**base, "sensor_class": "temperature", "sensor_deleted": 1}
        )
        is None
    )
    assert LibreNMSSensor.from_api({**base, "sensor_class": "state"}) is None


async def test_health_sensors_are_created(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Temperature is enabled; other classes are registered but off."""
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)

    temp = hass.states.get("sensor.core_sw01_system")
    assert temp is not None
    assert temp.state == "45.0"
    assert temp.attributes["device_class"] == SensorDeviceClass.TEMPERATURE
    assert temp.attributes["unit_of_measurement"] == "°C"
    assert temp.attributes["limit_high"] == 60.0
    assert temp.attributes["limit_low"] == 30.0

    # fanspeed and voltage exist but are not enabled by default.
    for entity_id in ("sensor.core_sw01_fan_1", "sensor.core_sw01_psu_1"):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None


async def test_implausible_sensor_is_unavailable_not_wrong(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A garbage reading must not reach the recorder as a real temperature."""
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get("sensor.garage_ap_temp1")
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_skipped_sensors_have_no_entity(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Deleted rows and state enums leave nothing behind in the registry."""
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)

    ids = {e.entity_id for e in registry.entities.values()}
    assert not any("retired_probe" in i for i in ids)
    assert not any("system_status" in i for i in ids)


async def test_health_sensor_recovers_when_the_reading_returns(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer,
) -> None:
    """The entity stays registered so a repaired sensor comes back on its own."""
    from .conftest import async_poll

    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.garage_ap_temp1").state == STATE_UNAVAILABLE

    sensors = [dict(s) for s in mock_librenms.sensors["sensors"]]
    for sensor in sensors:
        if sensor["sensor_id"] == 21:
            sensor["sensor_current"] = 39.5
    mock_librenms.set_sensors(sensors)
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.garage_ap_temp1").state == "39.5"


def _sensor_row(
    sensor_id: int, device_id: int, descr: str, value: float
) -> dict[str, Any]:
    """A sensor row shaped like LibreNMS's /resources/sensors output."""
    return {
        "sensor_id": sensor_id,
        "device_id": device_id,
        "sensor_class": "temperature",
        "sensor_type": "entity-sensor",
        "sensor_descr": descr,
        "sensor_index": str(sensor_id),
        "sensor_current": value,
        "sensor_limit": 60,
        "sensor_limit_low": 30,
        "sensor_divisor": 1,
        "sensor_multiplier": 1,
        "sensor_deleted": 0,
        "poller_type": "snmp",
    }


def _health_unique_ids(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    return [
        e.unique_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if "_sensor_" in e.unique_id
    ]


async def test_new_sensor_on_a_known_device_gets_an_entity(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A disk added to a device HA already knows appears on the next poll."""
    await setup_integration(hass, mock_config_entry)
    before = _health_unique_ids(hass, mock_config_entry)

    sensors = [dict(s) for s in mock_librenms.sensors["sensors"]]
    sensors.append(_sensor_row(99, 1, "New disk", 38))
    mock_librenms.set_sensors(sensors)
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_new_disk").state == "38.0"
    after = _health_unique_ids(hass, mock_config_entry)
    assert sorted(after) == sorted(
        [*before, f"{mock_config_entry.entry_id}_1_sensor_99"]
    )
    assert len(after) == len(set(after))


async def test_rediscovered_sensor_gets_a_new_entity_and_the_old_one_goes_away(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """LibreNMS rediscovery can reissue a sensor under a new sensor_id."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.core_sw01_system").state == "45.0"

    sensors = [
        _sensor_row(111, 1, "System", 46) if s["sensor_id"] == 11 else dict(s)
        for s in mock_librenms.sensors["sensors"]
    ]
    mock_librenms.set_sensors(sensors)
    await async_poll(hass, freezer)

    registry = er.async_get(hass)
    new_id = registry.async_get_entity_id(
        "sensor", "librenms", f"{mock_config_entry.entry_id}_1_sensor_111"
    )
    assert new_id is not None
    assert hass.states.get(new_id).state == "46.0"
    assert hass.states.get("sensor.core_sw01_system").state == STATE_UNAVAILABLE


async def test_sensor_that_returns_is_not_duplicated(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sensor missing for a poll reuses its entity when it comes back."""
    await setup_integration(hass, mock_config_entry)
    before = _health_unique_ids(hass, mock_config_entry)
    original = [dict(s) for s in mock_librenms.sensors["sensors"]]

    mock_librenms.set_sensors([s for s in original if s["sensor_id"] != 11])
    await async_poll(hass, freezer)
    assert hass.states.get("sensor.core_sw01_system").state == STATE_UNAVAILABLE

    mock_librenms.set_sensors(original)
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_system").state == "45.0"
    assert sorted(_health_unique_ids(hass, mock_config_entry)) == sorted(before)


async def test_sensors_missing_at_startup_are_created_when_they_arrive(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sensors that failed during setup get entities once they load."""
    mock_librenms.fail("sensors", status=500)
    await setup_integration(hass, mock_config_entry)
    assert _health_unique_ids(hass, mock_config_entry) == []

    mock_librenms.recover("sensors")
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_system").state == "45.0"
