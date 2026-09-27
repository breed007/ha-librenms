"""Tests for LibreNMS health sensors."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
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


# Rows shaped like LibreNMS 26.9 /resources/sensors output. The first two
# are the CPU clocks of a Raspberry Pi as LibreNMS's agent extend reports
# them (seen in a live lab). The rest are sensors from LibreNMS's own test
# data (tests/data at 26.9.1.1): frequency is stored in Hz, microwave and
# 60 GHz radios reach tens of GHz, and carrier offsets go negative.
PI_ARM_CLOCK = {
    "sensor_id": 101,
    "device_id": 1,
    "sensor_class": "frequency",
    "sensor_type": "raspberry_freq",
    "sensor_descr": "ARM",
    "sensor_index": "6",
    "sensor_current": 1500345728,
    "sensor_limit": 1575363014.4,
    "sensor_limit_low": 1425328441.6,
    "sensor_divisor": 1,
    "sensor_multiplier": 1,
    "sensor_deleted": 0,
    "poller_type": "snmp",
}
PI_CORE_CLOCK = {
    **PI_ARM_CLOCK,
    "sensor_id": 102,
    "sensor_descr": "Core",
    "sensor_index": "7",
    "sensor_current": 500000992,
    "sensor_limit": 525001041.6,
    "sensor_limit_low": 475000942.4,
}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (PI_ARM_CLOCK, 1500345728),
        (PI_CORE_CLOCK, 500000992),
        # tachyon: TACHYON-MIB::wirelessRadioFrequency, MHz x 1000000.
        (
            {
                **PI_ARM_CLOCK,
                "sensor_current": 66960000000,
                "sensor_multiplier": 1000000,
            },
            66960000000,
        ),
        # timos_7750: tmnxCohOptPortRxFreqOffset, negative.
        (
            {**PI_ARM_CLOCK, "sensor_current": -73000000, "sensor_multiplier": 1000000},
            -73000000,
        ),
        # apc: mains input frequency, divisor 10.
        ({**PI_ARM_CLOCK, "sensor_current": 49.9, "sensor_divisor": 10}, 49.9),
    ],
    ids=["pi_arm", "pi_core", "radio_67ghz", "negative_offset", "mains"],
)
def test_real_frequencies_are_readings(row: dict[str, Any], expected: float) -> None:
    """QA live lab, N-2: CPU clocks and radio frequencies are real values.

    The old 1 MHz ceiling threw away every CPU clock and radio frequency,
    and its lower bound of 0 threw away every negative offset.
    """
    sensor = LibreNMSSensor.from_api(row)
    assert sensor is not None
    assert sensor.implausible is False
    assert sensor.value == expected


@pytest.mark.parametrize(
    "row",
    [
        # uhp "Remote TTS": filed as frequency, raw 2^32 - 2145.
        {**PI_ARM_CLOCK, "sensor_current": 4294965151},
        # huaweiups_ups2000 runtime: 2^32 - 1 in a class with no own range.
        {
            **PI_ARM_CLOCK,
            "sensor_class": "runtime",
            "sensor_current": 4294967295,
        },
        # uhp "TX power level adjustment": 2^32 - 21 before a divisor of 10.
        {
            **PI_ARM_CLOCK,
            "sensor_class": "dbm",
            "sensor_current": 429496727.5,
            "sensor_divisor": 10,
        },
        # The documented temperature case, raw 2^32 - 263200 over 1000.
        {
            **PI_ARM_CLOCK,
            "sensor_class": "temperature",
            "sensor_current": 4294704.096,
            "sensor_divisor": 1000,
        },
    ],
    ids=["frequency_wrap", "runtime_all_ones", "dbm_wrap_divided", "temp_wrap"],
)
def test_wrapped_32_bit_readings_are_rejected(row: dict[str, Any]) -> None:
    """A raw value at the top of the 32-bit range is a sentinel in any class.

    These are the sentinels found in LibreNMS's own test data. A range per
    class cannot catch them all: 4294965151 Hz is a plausible radio
    frequency on its face, and runtime has no range of its own.
    """
    sensor = LibreNMSSensor.from_api(row)
    assert sensor is not None
    assert sensor.implausible is True
    assert sensor.value is None


# Power factor and load rows as they appear in LibreNMS's recorded test data
# at 26.9.1.1 (raritan-pdu_px3, sentry3_3phase, apc_smx750, vertiv-dcs).
RARITAN_OUTLET_PF = {
    **PI_ARM_CLOCK,
    "sensor_class": "power_factor",
    "sensor_type": "raritan-pdu",
    "sensor_descr": "22 DEVICE 2:Ps1:Installed",
    "sensor_index": "measurementsOutletSensorValue.1.22.7",
    "sensor_current": 86,
    "sensor_limit": 0,
    "sensor_limit_low": 0,
}
SENTRY_SYSTEM_PF = {
    **RARITAN_OUTLET_PF,
    "sensor_type": "sentry3",
    "sensor_descr": "System Power Factor",
    "sensor_index": "0",
    "sensor_current": 100,
    "sensor_limit": 1,
    "sensor_limit_low": -1,
}
APC_UPS_LOAD = {
    **PI_ARM_CLOCK,
    "sensor_class": "load",
    "sensor_type": "apc",
    "sensor_descr": "Load(VA)",
    "sensor_index": ".1.3.6.1.4.1.318.1.1.1.4.3.3.0",
    "sensor_current": 76.2,
    "sensor_divisor": 10,
    "sensor_limit": 80,
    "sensor_limit_low": None,
}


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # Raritan and Sentry report power factor on a 0 to 100 scale.
        (RARITAN_OUTLET_PF, 86),
        (SENTRY_SYSTEM_PF, 100),
        # Most devices use -1 to 1.
        ({**RARITAN_OUTLET_PF, "sensor_current": 0.95}, 0.95),
        ({**RARITAN_OUTLET_PF, "sensor_current": -0.5}, -0.5),
    ],
    ids=["raritan_86", "sentry_100", "fraction", "negative_fraction"],
)
def test_power_factor_on_either_scale_is_a_reading(
    row: dict[str, Any], expected: float
) -> None:
    """Power factor is shown as LibreNMS reports it, never rescaled.

    Some PDUs report 0 to 100 where most report -1 to 1. Rejecting the
    first scale threw away real readings and logged a false warning on
    every setup, the same shape of problem as the CPU clocks.
    """
    sensor = LibreNMSSensor.from_api(row)
    assert sensor is not None
    assert sensor.implausible is False
    assert sensor.value == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (APC_UPS_LOAD, 76.2),
        # A UPS in overload reports more than 100 percent, which is when
        # the reading matters most.
        ({**APC_UPS_LOAD, "sensor_current": 150}, 150),
    ],
    ids=["normal", "overload_150"],
)
def test_ups_overload_is_a_reading(row: dict[str, Any], expected: float) -> None:
    """Load above 100 percent is real on a UPS in overload."""
    sensor = LibreNMSSensor.from_api(row)
    assert sensor is not None
    assert sensor.implausible is False
    assert sensor.value == expected


@pytest.mark.parametrize(
    "row",
    [
        # vertiv-dcs "UPS Phase B Load": 9999 over a divisor of 10.
        {
            **APC_UPS_LOAD,
            "sensor_type": "vertiv-dcs",
            "sensor_descr": "UPS Phase B Load",
            "sensor_current": 999.9,
        },
        # 16-bit all ones, and the 32-bit sentinel, as a power factor.
        {**RARITAN_OUTLET_PF, "sensor_current": 65535},
        {**RARITAN_OUTLET_PF, "sensor_current": 4294967295},
    ],
    ids=["load_999_9", "power_factor_65535", "power_factor_wrap"],
)
def test_load_and_power_factor_sentinels_are_rejected(row: dict[str, Any]) -> None:
    """The wider bounds still reject the sentinels these classes produce."""
    sensor = LibreNMSSensor.from_api(row)
    assert sensor is not None
    assert sensor.implausible is True
    assert sensor.value is None


def test_converted_readings_are_left_to_the_range_check() -> None:
    """A `user_func` reading cannot be traced back to what the device sent.

    LibreNMS applies the conversion after the divisor and multiplier, so
    undoing those two says nothing about the device's value. Only the class
    range judges such a reading.
    """
    sensor = LibreNMSSensor.from_api(
        {
            **PI_ARM_CLOCK,
            "sensor_class": "runtime",
            "sensor_current": 4294967295,
            "user_func": "sec_to_min",
        }
    )
    assert sensor is not None
    assert sensor.implausible is False
    assert sensor.value == 4294967295


async def test_raspberry_pi_clocks_log_no_warning(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Pi's CPU clocks load as readings, without the implausible warning."""
    mock_librenms.set_sensors([PI_ARM_CLOCK, PI_CORE_CLOCK])
    await setup_integration(hass, mock_config_entry)

    sensors = mock_config_entry.runtime_data.data.sensors_by_device[1]
    assert [(s.value, s.implausible) for s in sensors] == [
        (1500345728, False),
        (500000992, False),
    ]
    assert "plausible range" not in caplog.text


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


async def test_retired_sensor_can_be_deleted_after_a_reload(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The documented way to clean up after rediscovery actually works.

    Registry entries are never removed automatically, because a sensor
    missing from one response looks exactly like a retired one and removal
    would throw away the user's customizations. After a reload the old
    entity is no longer provided, which is what lets Home Assistant offer to
    delete it, and its entry is still intact until the user does.
    """
    await setup_integration(hass, mock_config_entry)
    sensors = [
        _sensor_row(111, 1, "System", 46) if s["sensor_id"] == 11 else dict(s)
        for s in mock_librenms.sensors["sensors"]
    ]
    mock_librenms.set_sensors(sensors)
    await async_poll(hass, freezer)
    assert "restored" not in hass.states.get("sensor.core_sw01_system").attributes

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    old = hass.states.get("sensor.core_sw01_system")
    assert old.state == STATE_UNAVAILABLE
    assert old.attributes["restored"] is True
    assert er.async_get(hass).async_get("sensor.core_sw01_system") is not None
