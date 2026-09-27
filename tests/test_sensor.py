"""Tests for the LibreNMS sensor and binary sensor platforms."""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
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
        "acknowledged": False,
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
                "state": "1",
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


# LibreNMS polls each device every 300 s by default and writes `uptime` only
# then (LibreNMS/Modules/Core.php); between polls the value is frozen. Home
# Assistant polls every 60 s here, so it sees each value about five times.
LIBRENMS_POLL_INTERVAL = 300
HA_SCAN_INTERVAL = 60
CORE_SW01_UPTIME = 4321000


def _librenms_uptime(elapsed: int, phase: int, boot_age: int) -> int:
    """Return the uptime LibreNMS reports `elapsed` seconds into a test.

    LibreNMS polled the device at `phase`, `phase + 300`, ... seconds (and at
    `phase - 300` before the test began) and reports what it measured at the
    most recent of those. `boot_age` is the true uptime at elapsed == 0.
    """
    last_poll = (elapsed - phase) // LIBRENMS_POLL_INTERVAL * LIBRENMS_POLL_INTERVAL
    return boot_age + last_poll + phase


def _set_core_sw01_uptime(mock_librenms: MockLibreNMS, uptime: int) -> None:
    devices = [dict(d) for d in mock_librenms.devices["devices"]]
    devices[0]["uptime"] = uptime
    mock_librenms.set_devices(devices)


def _devices_polls(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(1 for call in aioclient_mock.mock_calls if "/devices" in str(call[1]))


@pytest.mark.parametrize("phase", [0, 17, 150, 299])
async def test_boot_time_holds_steady_under_the_real_librenms_cadence(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    phase: int,
) -> None:
    """A device that never reboots keeps one boot time for hours.

    Recomputing `now - uptime` on every poll produced a sawtooth here: 9 state
    changes in 15 minutes, jumping by up to 4 minutes at a time. The first
    reading can be up to one LibreNMS interval late, so it may be tightened
    once, the first time LibreNMS polls after setup. After that, nothing.
    """
    started = dt_util.utcnow()
    true_boot = started - timedelta(seconds=CORE_SW01_UPTIME)
    _set_core_sw01_uptime(mock_librenms, _librenms_uptime(0, phase, CORE_SW01_UPTIME))
    await setup_integration(hass, mock_config_entry)
    polls_before = _devices_polls(aioclient_mock)
    history = [hass.states.get("sensor.core_sw01_last_boot").state]

    for tick in range(1, 121):  # two hours of Home Assistant polling
        _set_core_sw01_uptime(
            mock_librenms,
            _librenms_uptime(tick * HA_SCAN_INTERVAL, phase, CORE_SW01_UPTIME),
        )
        await async_poll(hass, freezer, seconds=HA_SCAN_INTERVAL)
        history.append(hass.states.get("sensor.core_sw01_last_boot").state)

    assert _devices_polls(aioclient_mock) - polls_before == 120
    changed_at = [
        tick * HA_SCAN_INTERVAL
        for tick, (before, after) in enumerate(pairwise(history), start=1)
        if before != after
    ]
    assert len(changed_at) <= 1
    assert all(when <= LIBRENMS_POLL_INTERVAL + HA_SCAN_INTERVAL for when in changed_at)
    if phase == 0:
        # Setup coincided with a LibreNMS poll, so there is nothing to tighten.
        assert changed_at == []

    # Everything after the first LibreNMS cycle is completely still.
    settled = history[(LIBRENMS_POLL_INTERVAL + HA_SCAN_INTERVAL) // HA_SCAN_INTERVAL :]
    assert len(set(settled)) == 1

    # And the value it settles on is close to the real boot time: not before
    # it (allowing a second for the clock read at setup), and no later than
    # one Home Assistant poll after it.
    offset = (dt_util.parse_datetime(history[-1]) - true_boot).total_seconds()
    assert -1 <= offset <= HA_SCAN_INTERVAL + 1


async def test_boot_time_catches_up_after_a_stalled_poller(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reading an hour stale at startup is corrected once LibreNMS resumes.

    If Home Assistant starts while the LibreNMS poller is stuck, `uptime` is
    an hour old and the first estimate is an hour late. Holding it until the
    next reboot would leave it wrong for weeks.
    """
    true_boot = dt_util.utcnow() - timedelta(seconds=CORE_SW01_UPTIME)
    _set_core_sw01_uptime(mock_librenms, CORE_SW01_UPTIME - 3600)
    await setup_integration(hass, mock_config_entry)
    stale = dt_util.parse_datetime(hass.states.get("sensor.core_sw01_last_boot").state)
    assert (stale - true_boot).total_seconds() == pytest.approx(3600, abs=2)

    _set_core_sw01_uptime(mock_librenms, CORE_SW01_UPTIME + HA_SCAN_INTERVAL)
    await async_poll(hass, freezer, seconds=HA_SCAN_INTERVAL)

    fixed = dt_util.parse_datetime(hass.states.get("sensor.core_sw01_last_boot").state)
    assert abs((fixed - true_boot).total_seconds()) <= 2


async def test_unknown_uptime_is_not_a_boot_time(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """LibreNMS stores 0 when it cannot read uptime; that is not "just booted"."""
    _set_core_sw01_uptime(mock_librenms, 0)
    await setup_integration(hass, mock_config_entry)

    assert hass.states.get("sensor.core_sw01_last_boot").state == "unknown"


async def test_boot_time_moves_after_a_reboot(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A smaller uptime is a reboot, and shows up on the very next poll.

    This is the same rule LibreNMS uses to detect a reboot.
    """
    await setup_integration(hass, mock_config_entry)
    first = hass.states.get("sensor.core_sw01_last_boot").state

    _set_core_sw01_uptime(mock_librenms, 30)
    await async_poll(hass, freezer)

    rebooted = hass.states.get("sensor.core_sw01_last_boot").state
    assert rebooted != first
    expected = dt_util.utcnow() - timedelta(seconds=30)
    assert abs(dt_util.parse_datetime(rebooted) - expected) < timedelta(seconds=2)

    # The new boot time then holds under the normal cadence, like any other.
    for tick in range(1, 16):
        _set_core_sw01_uptime(
            mock_librenms,
            _librenms_uptime(tick * HA_SCAN_INTERVAL, 0, 30),
        )
        await async_poll(hass, freezer, seconds=HA_SCAN_INTERVAL)
        assert hass.states.get("sensor.core_sw01_last_boot").state == rebooted


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


async def test_poller_stale_stays_off_while_polling_progresses(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A moving `last_polled` keeps the sensor quiet, however much time passes."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "off"

    devices = list(mock_librenms.devices["devices"])
    for minute in range(1, 4):
        devices[0] = {**devices[0], "last_polled": f"2025-07-28 10:0{minute}:00"}
        mock_librenms.set_devices(devices)
        await async_poll(hass, freezer, seconds=601)

    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "off"


async def test_poller_stale_fires_when_the_marker_freezes(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An unchanged `last_polled` past the threshold means nothing is polling."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "off"
    assert hass.states.get("binary_sensor.librenms_problem").state == "on"

    # Devices keep reporting, but none of them has been polled since.
    await async_poll(hass, freezer, seconds=901)

    stale = hass.states.get("binary_sensor.librenms_poller_stale")
    assert stale.state == "on"
    assert stale.attributes["last_advanced"] == "2025-07-28 09:14:03"
    assert stale.attributes["stalled_for_seconds"] >= 900


async def test_poller_stale_clears_when_polling_resumes(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Recovery is detected on the first poll that advances the marker."""
    await setup_integration(hass, mock_config_entry)
    await async_poll(hass, freezer, seconds=901)
    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "on"

    devices = list(mock_librenms.devices["devices"])
    devices[0] = {**devices[0], "last_polled": "2025-07-28 11:00:00"}
    mock_librenms.set_devices(devices)
    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "off"


async def test_poller_stale_drives_the_problem_sensor(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A silent instance must not read as healthy.

    Everything else is green here -- no devices down, no alerts -- which is
    exactly the state a dead poller produces.
    """
    devices = [
        {**d, "status": 1, "status_reason": ""}
        for d in mock_librenms.devices["devices"]
    ]
    mock_librenms.set_devices(devices)
    mock_librenms.set_alerts([])
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.librenms_problem").state == "off"

    await async_poll(hass, freezer, seconds=901)

    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "on"
    assert hass.states.get("binary_sensor.librenms_problem").state == "on"


async def test_poller_stale_is_unjudged_without_poll_times(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """No `last_polled` anywhere means there is nothing to judge, not a fault."""
    mock_librenms.set_devices(
        [{"device_id": 1, "hostname": "sw01", "status": 1, "icon": "ubiquiti.svg"}]
    )
    await setup_integration(hass, mock_config_entry)
    await async_poll(hass, freezer, seconds=901)

    stale = hass.states.get("binary_sensor.librenms_poller_stale")
    assert stale.state == "off"
    assert stale.attributes["last_advanced"] is None
