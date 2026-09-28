"""Tests for health sensor names on devices where descriptions repeat.

LibreNMS often gives several sensors on one device the same description:
a Synology's disk temperature and bad-sector count are both "Disk 1
DT01ACA300", and a MikroTik's PoE current, power and voltage are all
"ether1 POE". Rows here are shaped like LibreNMS 26.9 /resources/sensors
output, taken from its recorded test data (dsm_ds214se-dsm7,
routeros_rb5009upr+s+, bdcom) and from a live Raspberry Pi agent.
"""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.const import DOMAIN
from custom_components.librenms.coordinator import LibreNMSSensor

from .conftest import MockLibreNMS, async_poll, setup_integration


def _row(sensor_id: int, sensor_class: str, descr: str, **extra: Any) -> dict:
    return {
        "sensor_id": sensor_id,
        "device_id": 1,
        "sensor_class": sensor_class,
        "sensor_descr": descr,
        "sensor_current": 1,
        "sensor_divisor": 1,
        "sensor_multiplier": 1,
        "sensor_deleted": 0,
        "poller_type": "snmp",
        **extra,
    }


# dsm_ds214se-dsm7: temperature group "Disks", count group "Bad sectors".
SYNOLOGY = [
    _row(67, "count", "Disk 1 DT01ACA300", group="Bad sectors", sensor_index="0"),
    _row(81, "temperature", "Disk 1 DT01ACA300", group="Disks", sensor_index="0"),
]
# routeros_rb5009upr+s+: all three in group "Ports".
MIKROTIK = [
    _row(90, "current", "ether1 POE", group="Ports", sensor_index="mtxrPOECurrent.2"),
    _row(91, "power", "ether1 POE", group="Ports", sensor_index="mtxrPOEPower.2"),
    _row(92, "voltage", "ether1 POE", group="Ports", sensor_index="mtxrPOEVoltage.2"),
]


def _names(rows: list[dict]) -> dict[int, str]:
    from custom_components.librenms.coordinator import name_sensors

    sensors = [LibreNMSSensor.from_api(row) for row in rows]
    name_sensors(sensors)
    return {sensor.sensor_id: sensor.name for sensor in sensors}


def test_unique_descriptions_keep_their_names() -> None:
    """Only sensors that share a description get anything added."""
    rows = [*SYNOLOGY, _row(11, "temperature", "System")]
    assert _names(rows)[11] == "System"


def test_shared_descriptions_gain_the_sensor_class() -> None:
    """QA live lab, N-1: the class is what tells these apart."""
    assert _names(SYNOLOGY + MIKROTIK) == {
        67: "Disk 1 DT01ACA300 count",
        81: "Disk 1 DT01ACA300 temperature",
        90: "ether1 POE current",
        91: "ether1 POE power",
        92: "ether1 POE voltage",
    }


def test_same_class_duplicates_use_the_group_when_it_differs() -> None:
    """bdcom: two counts described "110", told apart only by their group."""
    rows = [
        _row(5, "count", "110", group="GPON Active Onu Num"),
        _row(6, "count", "110", group="GPON Inactive Onu Num"),
    ]
    assert _names(rows) == {
        5: "110 GPON Active Onu Num",
        6: "110 GPON Inactive Onu Num",
    }


def test_same_class_duplicates_are_numbered_otherwise() -> None:
    """With nothing else to go on, a number in LibreNMS sensor id order."""
    rows = [
        _row(22, "temperature", "Temp", group="System"),
        _row(21, "temperature", "Temp", group="System"),
        _row(30, "voltage", "Temp"),
    ]
    assert _names(rows) == {
        21: "Temp temperature 1",
        22: "Temp temperature 2",
        30: "Temp voltage",
    }


async def test_new_install_gets_no_numbered_entity_ids(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """On a first install every entity id says what the sensor measures."""
    mock_librenms.set_sensors([*SYNOLOGY, *MIKROTIK])
    await setup_integration(hass, mock_config_entry)

    registry = er.async_get(hass)
    entity_ids = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(
            registry, mock_config_entry.entry_id
        )
        if entry.domain == "sensor" and "_sensor_" in entry.unique_id
    }
    assert entity_ids == {
        "sensor.core_sw01_disk_1_dt01aca300_count",
        "sensor.core_sw01_disk_1_dt01aca300_temperature",
        "sensor.core_sw01_ether1_poe_current",
        "sensor.core_sw01_ether1_poe_power",
        "sensor.core_sw01_ether1_poe_voltage",
    }
    state = hass.states.get("sensor.core_sw01_disk_1_dt01aca300_temperature")
    assert (
        state.attributes["friendly_name"] == "core-sw01 Disk 1 DT01ACA300 temperature"
    )


async def test_existing_install_keeps_its_entity_ids(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An upgrade changes friendly names only, never an entity id.

    0.3.1 named both Synology sensors "Disk 1 DT01ACA300", so the count
    (lower id) took the plain entity id and the temperature got `_2`. Those
    entries are already in the registry, keyed by unique id, and must stay.
    """
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old_ids = {
        67: "sensor.core_sw01_disk_1_dt01aca300",
        81: "sensor.core_sw01_disk_1_dt01aca300_2",
    }
    for sensor_id, entity_id in old_ids.items():
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{mock_config_entry.entry_id}_1_sensor_{sensor_id}",
            suggested_object_id=entity_id.removeprefix("sensor."),
            config_entry=mock_config_entry,
        )
    mock_librenms.set_sensors(SYNOLOGY)
    await setup_integration(hass, mock_config_entry)

    for sensor_id, entity_id in old_ids.items():
        unique_id = f"{mock_config_entry.entry_id}_1_sensor_{sensor_id}"
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
    assert hass.states.get(old_ids[81]).attributes["friendly_name"] == (
        "core-sw01 Disk 1 DT01ACA300 temperature"
    )
    assert hass.states.get(old_ids[67]).attributes["friendly_name"] == (
        "core-sw01 Disk 1 DT01ACA300 count"
    )


async def test_a_new_namesake_renames_without_moving_the_entity(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sensor that later shares a description changes friendly names only.

    The Pi's "Core" frequency exists first; LibreNMS then discovers a "Core"
    voltage on the same device. The frequency entity keeps its id.
    """
    core_clock = _row(102, "frequency", "Core")
    mock_librenms.set_sensors([core_clock])
    await setup_integration(hass, mock_config_entry)
    registry = er.async_get(hass)
    unique_id = f"{mock_config_entry.entry_id}_1_sensor_102"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id == "sensor.core_sw01_core"
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_librenms.set_sensors([core_clock, _row(110, "voltage", "Core")])
    await async_poll(hass, freezer)

    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
    assert hass.states.get(entity_id).attributes["friendly_name"] == (
        "core-sw01 Core frequency"
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{mock_config_entry.entry_id}_1_sensor_110"
        )
        == "sensor.core_sw01_core_voltage"
    )
