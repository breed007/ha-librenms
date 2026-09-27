"""Tests for partial failures: optional sensors, and 401 versus 403."""

from __future__ import annotations

import logging
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.const import DOMAIN

from .conftest import MockLibreNMS, async_poll, setup_integration

SENSOR_FAILURES: dict[str, dict[str, Any]] = {
    "http_500": {"status": 500, "message": "Server Error"},
    "http_403": {"status": 403, "message": "Insufficient permissions"},
    "timeout": {"exc": TimeoutError()},
    "bad_json": {"text": "<html><body>502 Bad Gateway</body></html>"},
}


def _reauth_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


@pytest.mark.parametrize("failure", SENSOR_FAILURES.values(), ids=SENSOR_FAILURES)
async def test_sensor_failure_leaves_the_core_working(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    failure: dict[str, Any],
) -> None:
    """Only health sensors go down; up/down, alerts and problem carry on."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.core_sw01_system").state == "45.0"

    mock_librenms.fail("sensors", **failure)
    with caplog.at_level(logging.WARNING, logger="custom_components.librenms"):
        for _ in range(3):
            await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"
    assert hass.states.get("binary_sensor.garage_ap_status").state == "off"
    assert hass.states.get("binary_sensor.librenms_problem").state == "on"
    assert hass.states.get("sensor.librenms_active_alerts").state == "2"
    assert hass.states.get("sensor.core_sw01_last_boot").state != STATE_UNAVAILABLE

    # A stale reading presented as current would be worse than no reading.
    assert hass.states.get("sensor.core_sw01_system").state == STATE_UNAVAILABLE

    # Logged once for the outage, not once per poll.
    warnings = [
        r for r in caplog.records if "health sensors are unavailable" in r.message
    ]
    assert len(warnings) == 1

    assert _reauth_flows(hass) == []


async def test_sensors_recover_on_their_own(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The next good poll brings readings back and says so once."""
    await setup_integration(hass, mock_config_entry)
    mock_librenms.fail("sensors", status=500)
    await async_poll(hass, freezer)
    assert hass.states.get("sensor.core_sw01_system").state == STATE_UNAVAILABLE

    mock_librenms.recover("sensors")
    with caplog.at_level(logging.INFO, logger="custom_components.librenms"):
        await async_poll(hass, freezer)
        await async_poll(hass, freezer)

    assert hass.states.get("sensor.core_sw01_system").state == "45.0"
    recovered = [r for r in caplog.records if "available again" in r.message]
    assert len(recovered) == 1


async def test_sensors_failing_at_startup_do_not_block_setup(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Device and alert entities come up even when sensors never answered."""
    mock_librenms.fail("sensors", status=500)
    assert await setup_integration(hass, mock_config_entry)

    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"
    assert hass.states.get("sensor.librenms_devices").state == "2"


async def test_no_sensors_at_all_is_not_a_failure(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LibreNMS answers 404 "Sensors do not exist" when there are none."""
    mock_librenms.fail("sensors", status=404, message="Sensors do not exist")

    with caplog.at_level(logging.WARNING, logger="custom_components.librenms"):
        await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.data.sensors_available is True
    assert mock_config_entry.runtime_data.data.sensors_by_device == {}
    assert not [r for r in caplog.records if "health sensors" in r.message]


@pytest.mark.parametrize("endpoint", ["devices", "alerts"])
async def test_core_403_raises_a_repair_issue_not_reauth(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    endpoint: str,
) -> None:
    """A role problem is explained, and never loops through reauthentication.

    Reauth cannot fix a 403: re-entering the same valid token passes
    validation, reloads, and hits the same 403 again.
    """
    await setup_integration(hass, mock_config_entry)
    issue_id = f"insufficient_permissions_{mock_config_entry.entry_id}"

    mock_librenms.fail(endpoint, status=403, message="Insufficient permissions")
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert _reauth_flows(hass) == []
    assert hass.states.get("binary_sensor.core_sw01_status").state == (
        STATE_UNAVAILABLE
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "insufficient_permissions"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {"url": "https://librenms.example.com"}

    mock_librenms.recover(endpoint)
    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_core_401_still_starts_reauth(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A token LibreNMS no longer accepts is what reauth exists for."""
    await setup_integration(hass, mock_config_entry)

    mock_librenms.fail("devices", status=401, message="Unauthenticated.")
    await async_poll(hass, freezer)

    assert len(_reauth_flows(hass)) == 1


async def test_permission_issue_is_removed_with_the_entry(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Deleting the entry does not leave an orphaned repair behind."""
    await setup_integration(hass, mock_config_entry)
    mock_librenms.fail("devices", status=403)
    await async_poll(hass, freezer)
    issue_id = f"insufficient_permissions_{mock_config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_empty_device_list_is_accepted_once_it_persists(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An instance that really was emptied is not failed forever.

    The first empty responses are treated as a failed update; once the list
    has been empty for three polls in a row it is taken at its word.
    """
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_devices([])

    await async_poll(hass, freezer)
    await async_poll(hass, freezer)
    assert hass.states.get("sensor.librenms_devices").state == STATE_UNAVAILABLE

    await async_poll(hass, freezer)
    assert hass.states.get("sensor.librenms_devices").state == "0"
    await async_poll(hass, freezer)
    assert hass.states.get("sensor.librenms_devices").state == "0"


async def test_empty_device_list_at_startup_is_accepted(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """With no earlier list to contradict it, an empty one is simply empty."""
    mock_librenms.set_devices([])
    assert await setup_integration(hass, mock_config_entry)

    assert hass.states.get("sensor.librenms_devices").state == "0"


async def test_one_empty_device_list_does_not_reset_the_count(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Empty, good, empty is two separate blips, not a persistent empty list."""
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices

    for _ in range(3):
        mock_librenms.set_devices([])
        await async_poll(hass, freezer)
        assert hass.states.get("sensor.librenms_devices").state == (STATE_UNAVAILABLE)
        mock_librenms.devices = good
        await async_poll(hass, freezer)
        assert hass.states.get("sensor.librenms_devices").state == "2"
